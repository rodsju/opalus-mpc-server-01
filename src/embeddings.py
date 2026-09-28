"""Vetor da pergunta: o único embedding que este servidor gera.

Recorte de leitura do módulo homônimo do opalus-data-connect. Lá o módulo
vetoriza o catálogo inteiro e grava em catalogo.objeto.embedding e
catalogo.coluna.embedding; aqui só se vetoriza o termo que o agente perguntou,
para compará-lo com o que já está gravado. Nada é escrito.

Um texto, um vetor, sem chunking -- o termo de busca é curto por natureza.
"""

import logging
import random
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)

# Tentativas por chamada, com espera exponencial.
TENTATIVAS = 4
ESPERA_BASE = 2.0

# Vetores de termo guardados em memória, por (modelo, termo).
#
# Cada busca é uma chamada PAGA de embedding, e um agente não pergunta uma vez:
# ele refina ("pacientes internados" -> "internação de pacientes" -> volta),
# repete a mesma busca depois de olhar outra tabela, e reexecuta a conversa
# inteira quando o contexto é resumido. O termo repetido é o caso comum, não a
# exceção -- e o vetor de um mesmo texto no mesmo modelo é sempre o mesmo.
#
# O teto existe porque o valor é grande: 4096 floats viram ~50 KB de literal.
# 128 entradas ≈ 6 MB, e o processo do MCP não cresce além disso.
CACHE_MAXIMO = 128
_cache = OrderedDict()


class EmbutirFalhou(Exception):
    """Motivo curto + detalhe acionável -- o mesmo contrato de ConfiguracaoInvalida."""

    def __init__(self, motivo, detalhe=""):
        super().__init__(motivo)
        self.motivo = motivo
        self.detalhe = detalhe


def vetor_para_pg(vetor):
    """[0.1, 0.2] -> "[0.1,0.2]", o literal que o cast ::vector aceita.

    Evita a dependência do pacote pgvector só para tipar o parâmetro no psycopg.
    """
    return "[" + ",".join(repr(float(x)) for x in vetor) + "]"


def _chamar(cliente, textos, prov):
    """Uma chamada, com retry. Devolve os vetores na ordem dos textos."""
    import openai

    for tentativa in range(1, TENTATIVAS + 1):
        try:
            resposta = cliente.embeddings.create(model=prov.modelo, input=textos)
            break
        except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError,
                openai.InternalServerError) as falha:
            if tentativa == TENTATIVAS:
                raise EmbutirFalhou(
                    "O provedor de embeddings não respondeu",
                    f"{type(falha).__name__} em {TENTATIVAS} tentativas: {falha}",
                ) from falha
            espera = ESPERA_BASE ** tentativa + random.uniform(0, 1)
            logger.warning(
                "embeddings: %s na tentativa %s de %s, repetindo em %.1fs",
                type(falha).__name__, tentativa, TENTATIVAS, espera,
            )
            time.sleep(espera)
        except openai.APIStatusError as falha:
            raise EmbutirFalhou(
                "O provedor de embeddings recusou a chamada",
                f"HTTP {falha.status_code}: {falha}",
            ) from falha

    # A API não promete devolver na ordem em que mandamos; o campo index é quem
    # promete. Com um texto só isso não muda nada, mas a garantia é de graça.
    dados = sorted(resposta.data, key=lambda item: item.index)
    return [item.embedding for item in dados]


def embutir(textos, prov):
    """Vetoriza a lista. Devolve (vetores, dimensão). Sem cache -- uso interno."""
    if prov.tipo != "openai":
        raise EmbutirFalhou(
            f"O provedor '{prov.nome}' não serve para embeddings",
            "A API de embeddings segue o formato OpenAI; a Anthropic não tem uma. "
            "Cadastre o provedor com tipo 'openai' em Setup › AI Providers.",
        )
    if not textos:
        return [], None

    import openai

    cliente = openai.OpenAI(api_key=prov.chave, base_url=prov.url or None)
    vetores = _chamar(cliente, textos, prov)

    dimensoes = {len(vetor) for vetor in vetores}
    if len(dimensoes) > 1:
        raise EmbutirFalhou(
            "O provedor devolveu vetores de dimensões diferentes",
            f"dimensões recebidas: {sorted(dimensoes)}.",
        )
    return vetores, dimensoes.pop()


def vetor_da_consulta(termo, prov):
    """Literal ::vector do termo, servido do cache quando o texto se repete.

    É esta a entrada que src/busca.py usa -- `embutir` fica exposto só para quem
    precisar vetorizar algo fora do caminho da busca.
    """
    chave = (prov.modelo, termo)
    if chave in _cache:
        _cache.move_to_end(chave)
        logger.info("vetor de '%s' veio do cache (%s)", termo, prov.modelo)
        return _cache[chave]

    vetores, _ = embutir([termo], prov)
    literal = vetor_para_pg(vetores[0])

    _cache[chave] = literal
    if len(_cache) > CACHE_MAXIMO:
        _cache.popitem(last=False)
    return literal

