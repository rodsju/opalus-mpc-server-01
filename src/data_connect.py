"""Envio de descrição curada para a API do opalus-data-connect.

Este é o único módulo que ESCREVE alguma coisa, e ele escreve por HTTP, não por
SQL. A regra do projeto continua valendo: o servidor MCP lê o catálogo
(src/catalogo.py, no Postgres) e lê o dado (src/oracle.py, no Oracle, em sessão
READ ONLY). Quem grava o catálogo é o opalus-data-connect.

O caminho contrário -- este servidor dando UPDATE direto em catalogo.descricao_ia
-- foi descartado, e a razão está do lado de lá: gravar o texto é metade do
trabalho. A outra metade é refazer o vetor daquele texto, com o modelo que a
configuração do app escolheu, e mexer no jsonb certo (descricao_ia.campos, não
coluna.descricao_ia, que a próxima vetorização sobrescreve). Duplicar essa regra
aqui seria duplicar a parte do sistema que falha em silêncio: vetor velho
apontando para texto novo não dá erro, só piora a busca.

Dois endpoints, um para cada tool:

    POST /api/objetos/descricoes    -- a tabela: rótulo, resumo, função, domínio
    POST /api/dicionario/descricoes -- o dicionário: uma linha por (tabela, coluna)

A semântica dos dois, em uma frase: o que o corpo não traz não muda. É por isso
que este módulo monta o JSON com só as chaves que o chamador informou -- mandar
`"funcao": ""` num corpo que queria falar só do rótulo APAGARIA a função.

O corpo é validado com extra="forbid" do outro lado: chave que não existe no
contrato vira 422, não some em silêncio. Ao mexer aqui, confira os nomes em
opalus-data-connect/src/api.py.
"""

import logging
import os

logger = logging.getLogger(__name__)

ROTA_OBJETOS = "/api/objetos/descricoes"
ROTA_DICIONARIO = "/api/dicionario/descricoes"

# Teto de colunas por chamada. Espelha TETO_COLUNAS da API: conferir aqui evita
# uma ida até lá para receber 400.
LIMITE_CAMPOS = 500

# Teto de texto. Não é limite do banco: é o tamanho em que uma descrição ainda é
# descrição. O rótulo é a legenda de listagem (o catálogo pede até 5 palavras);
# o resumo que passa de ~2 mil caracteres virou documentação e não cabe numa
# linha de dicionário nem num vetor útil.
LIMITE_ROTULO = 120
LIMITE_DESCRICAO = 2000

# Generoso porque a chamada fica aberta até a última vetorização: a API grava e
# só então vai à API de embeddings, uma vez por texto que mudou. Uma tabela de
# 300 campos descritos de uma vez passa longe de 30s.
TIMEOUT_PADRAO = 180.0


class ConfiguracaoInvalida(Exception):
    def __init__(self, motivo, detalhe=""):
        super().__init__(motivo)
        self.motivo = motivo
        self.detalhe = detalhe


class TextoInvalido(Exception):
    """Recusado antes de sair daqui -- não chegou a virar chamada HTTP."""

    def __init__(self, motivo, detalhe=""):
        super().__init__(motivo)
        self.motivo = motivo
        self.detalhe = detalhe


class DataConnectIndisponivel(Exception):
    """Não houve resposta: host errado, container fora, rede sem rota."""

    def __init__(self, motivo, detalhe=""):
        super().__init__(motivo)
        self.motivo = motivo
        self.detalhe = detalhe


class EnvioRecusado(Exception):
    """A API respondeu, e respondeu não (400/422/502...)."""

    def __init__(self, motivo, detalhe="", status=None, corpo=None):
        super().__init__(motivo)
        self.motivo = motivo
        self.detalhe = detalhe
        self.status = status
        self.corpo = corpo


def _base():
    """URL do opalus-data-connect, sem barra no fim."""
    url = (os.getenv("DATA_CONNECT_URL") or "").strip().rstrip("/")
    if not url:
        raise ConfiguracaoInvalida(
            "DATA_CONNECT_URL não configurada",
            "Aponte para o opalus-data-connect no .env. Entre containers da mesma "
            "rede é http://app:8080; de fora do compose, http://localhost:8081.",
        )
    return url


def _cabecalhos():
    """Quem está editando, e o token se algum dia a API exigir um.

    O email vai no `x-opalus-user-email` porque é o header que o próprio
    opalus-data-connect usa para identificar quem está na tela (src/usuario.py), e
    é dele que sai o `editado_por` gravado na descrição. A API não exige -- mas
    edição de dicionário sem autor é o tipo de coisa que ninguém audita depois.
    """
    cabecalhos = {"Content-Type": "application/json"}
    token = (os.getenv("DATA_CONNECT_TOKEN") or "").strip()
    if token:
        cabecalhos["Authorization"] = f"Bearer {token}"
    email = (os.getenv("DATA_CONNECT_USER_EMAIL") or os.getenv("x-opalus-user-email") or "").strip()
    if email:
        cabecalhos["x-opalus-user-email"] = email
    return cabecalhos


def _texto(valor, limite, rotulo_do_campo):
    """Normaliza e valida. Devolve None quando o chamador não informou nada.

    None aqui vira AUSÊNCIA no JSON, que a API lê como "não mexa neste campo".
    String em branco é recusada de propósito: para a API ela significa APAGAR, e
    apagar por acidente um texto que alguém escreveu é caro demais para ser o
    efeito de um parâmetro esquecido. Quem quiser limpar, limpa pela tela.
    """
    if valor is None:
        return None
    texto = valor.strip()
    if not texto:
        return None
    if len(texto) > limite:
        raise TextoInvalido(
            f"{rotulo_do_campo} tem {len(texto)} caracteres, o máximo é {limite}",
            "Descrição de dicionário é uma linha, não um documento: diga o que o "
            "dado é e para que serve, e deixe o detalhe para quem for consultar.",
        )
    return texto


def _detalhar_falha(dados):
    """Tira da resposta o motivo item a item, em vez de devolver o dict cru.

    A API responde em lote mesmo quando se manda um objeto só, então o erro útil
    está em objetos[0].erro -- e não no envelope. `criada` presente no item é o
    que diz que o TEXTO FOI GRAVADO e só a vetorização falhou: nesse caso repetir
    a chamada não é necessário, e a próxima vetorização refaz o vetor sozinha.
    """
    if not isinstance(dados, dict):
        return str(dados)[:500], False

    itens = dados.get("objetos")
    if isinstance(itens, list) and itens:
        partes, gravou = [], False
        for item in itens:
            if item.get("ok"):
                continue
            erro = item.get("erro") or {}
            nome = item.get("nome", "?")
            texto = f"{nome}: {erro.get('motivo', '?')}"
            if erro.get("detalhe"):
                texto += f" -- {erro['detalhe']}"
            # Item com `criada` passou pela gravação; o que falhou foi o vetor.
            if "criada" in item:
                gravou = True
                texto += " (o TEXTO foi gravado; o que falhou foi o vetor)"
            partes.append(texto)
        if partes:
            return " | ".join(partes)[:800], gravou

    # 422 do pydantic e os erros de envelope não têm `objetos`.
    for chave in ("detalhe", "erro", "detail", "motivo"):
        if dados.get(chave):
            return str(dados[chave])[:500], False
    return str(dados)[:500], False


def _enviar(rota, corpo, descricao):
    """Uma chamada. Devolve o corpo da resposta já em dict."""
    import httpx

    url = _base() + rota
    timeout = float(os.getenv("DATA_CONNECT_TIMEOUT", TIMEOUT_PADRAO))
    logger.info("POST %s: %s", url, corpo)

    try:
        resposta = httpx.post(url, json=corpo, headers=_cabecalhos(), timeout=timeout)
    except httpx.TimeoutException as falha:
        raise DataConnectIndisponivel(
            f"o opalus-data-connect não respondeu em {timeout:.0f}s",
            f"{descricao}: a gravação pode ter acontecido mesmo assim -- a API "
            "grava antes de vetorizar, e a vetorização é a parte demorada. "
            "Confira com describe_catalog_table antes de repetir.",
        ) from falha
    except httpx.HTTPError as falha:
        raise DataConnectIndisponivel(
            "não foi possível falar com o opalus-data-connect",
            f"{type(falha).__name__} em {url}: {falha}. Confira DATA_CONNECT_URL "
            "e se o container está de pé na mesma rede deste servidor.",
        ) from falha

    try:
        dados = resposta.json()
    except ValueError:
        dados = {"resposta": resposta.text[:500]}

    # 200 é tudo certo; 207 é lote misto; o resto é recusa. Num lote misto o
    # corpo diz item a item quem passou, então ele sobe como erro com o detalhe
    # de quem falhou -- e o `gravados` do envelope mostra quantos entraram.
    if resposta.status_code >= 400 or resposta.status_code == 207:
        detalhe, gravou = _detalhar_falha(dados)
        motivo = (
            "parte do lote não foi gravada"
            if resposta.status_code == 207
            else f"a API recusou a gravação (HTTP {resposta.status_code})"
        )
        if gravou and resposta.status_code == 502:
            motivo = "o texto foi gravado, mas o vetor de busca não foi refeito"
        raise EnvioRecusado(motivo, detalhe, status=resposta.status_code, corpo=dados)

    logger.info("%s gravada (HTTP %s)", descricao, resposta.status_code)
    return dados


def _resultado(dados):
    """O que interessa da resposta, sem o envelope de lote de um item só."""
    itens = dados.get("objetos") or []
    item = itens[0] if len(itens) == 1 else None
    resumo = {
        "ok": True,
        "gravados": dados.get("gravados"),
        "modelo_de_vetor": dados.get("modelo_de_vetor"),
    }
    if item:
        resumo["criada"] = item.get("criada")
        resumo["vetores"] = item.get("vetores")
    else:
        resumo["objetos"] = itens
    return resumo


# ---------------------------------------------------------------------------
# As duas operações
# ---------------------------------------------------------------------------


def atualizar_tabela(tabela, resumo=None, rotulo=None, funcao=None, dominio=None):
    """Rótulo, resumo, função e domínio de uma TABELA. Só o informado muda."""
    nome = (tabela or "").strip()
    if not nome:
        raise TextoInvalido("o nome da tabela está vazio")

    objeto = {"nome": nome}
    for chave, valor, limite, legenda in (
        ("rotulo", rotulo, LIMITE_ROTULO, "o rótulo"),
        ("resumo", resumo, LIMITE_DESCRICAO, "o resumo"),
        ("funcao", funcao, LIMITE_DESCRICAO, "a função"),
        ("dominio", dominio, LIMITE_ROTULO, "o domínio"),
    ):
        texto = _texto(valor, limite, legenda)
        if texto is not None:
            objeto[chave] = texto

    if len(objeto) == 1:
        raise TextoInvalido(
            "nada para gravar",
            "Informe ao menos um entre resumo, rotulo, funcao e dominio.",
        )

    dados = _enviar(ROTA_OBJETOS, {"objetos": [objeto]}, f"descrição de {nome}")
    return {"tabela": nome, "enviado": objeto, **_resultado(dados)}


def atualizar_campos(tabela, campos):
    """Descrição de um ou vários CAMPOS de uma tabela, no formato do dicionário.

    `campos` é {coluna: descrição}. Vai para /api/dicionario/descricoes como uma
    linha por coluna -- a mesma forma da planilha que sai de vw_dicionario.
    """
    import json

    nome = (tabela or "").strip()
    if not nome:
        raise TextoInvalido("o nome da tabela está vazio")

    # Cliente de MCP às vezes serializa o objeto em string antes de mandar.
    if isinstance(campos, str):
        try:
            campos = json.loads(campos)
        except ValueError as falha:
            raise TextoInvalido(
                "`campos` não é um objeto JSON válido",
                'Mande {"COLUNA": "descrição", ...}.',
            ) from falha
    if not isinstance(campos, dict) or not campos:
        raise TextoInvalido(
            "`campos` está vazio ou não é um objeto",
            'Mande {"COLUNA": "descrição", ...} com ao menos uma coluna.',
        )
    if len(campos) > LIMITE_CAMPOS:
        raise TextoInvalido(
            f"são {len(campos)} campos, o máximo por chamada é {LIMITE_CAMPOS}",
            "Quebre em chamadas menores.",
        )

    linhas, vazios = [], []
    for coluna, descricao in campos.items():
        nome_coluna = (coluna or "").strip()
        if not nome_coluna:
            raise TextoInvalido("há um nome de coluna em branco em `campos`")
        texto = _texto(descricao, LIMITE_DESCRICAO, f"a descrição de {nome_coluna}")
        if texto is None:
            # Em branco APAGA do lado de lá; aqui é quase sempre engano de quem
            # montou o dicionário, então some da chamada e volta relatado.
            vazios.append(nome_coluna)
            continue
        linhas.append({"objeto": nome, "coluna": nome_coluna, "descricao": texto})

    if not linhas:
        raise TextoInvalido(
            "nenhum campo com descrição",
            f"Vieram só descrições em branco ({', '.join(vazios)}). Texto em "
            "branco apagaria a descrição atual, e isso não se faz por aqui.",
        )

    dados = _enviar(
        ROTA_DICIONARIO, {"colunas": linhas}, f"{len(linhas)} campos de {nome}"
    )
    resultado = {
        "tabela": nome,
        "campos_enviados": [linha["coluna"] for linha in linhas],
        **_resultado(dados),
    }
    if vazios:
        resultado["ignorados_por_estarem_em_branco"] = vazios
    return resultado
