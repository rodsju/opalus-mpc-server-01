"""Busca semântica sobre os vetores do catálogo.

Este módulo é a CONSULTA; src/main.py expõe a tool MCP -- a mesma divisão que a
origem (~/apps/opalus-data-connect/src/busca.py, onde quem consumia era a tela).

Por que o ranking não é só cosseno. Medindo 300 campos tomados ao acaso deste
catálogo, dois campos SEM relação nenhuma já pontuam:

    mínimo 0.155 | média 0.456 | p95 0.637 | máximo 0.975

    -- a consulta que mede, para refazer quando trocar de modelo:
    -- WITH amostra AS (SELECT embedding v FROM catalogo.coluna
    --                   WHERE embedding IS NOT NULL ORDER BY random() LIMIT 300),
    --      a AS (SELECT v, row_number() OVER () r FROM amostra)
    -- SELECT avg(1-(x.v <=> y.v)) FROM a x JOIN a y ON x.r < y.r;

Disso vem o desenho todo:

1. Cosseno cru engana. 0.65 parece bom e é ruído, então o que sai daqui não é o
   cosseno: é uma relevância reescalada sobre essa linha de base.
2. Só semântica erra o óbvio -- quem digita TDPACIENTE quer aquela tabela em
   primeiro lugar. Daí o reforço lexical somado ao cosseno.

O consumidor mudou de tela para agente, e três coisas mudam com ele:

- quem lê o resultado não sabe descartar ruído. A tela mostrava 20 linhas e o
  olho pulava as ruins; um LLM cita a pior delas com a mesma convicção. Por isso
  MINIMO_RELEVANCIA corta o rabo da lista antes de devolver.
- o custo da chamada de embedding fica em src/embeddings.py, com cache: agente
  refina e repete termo muito mais do que gente clicando.
- cos e score saem do retorno e ficam no log. São para depurar ranking, não para
  ocupar contexto de quem vai responder.
"""

import logging
import time
import unicodedata

from . import catalogo, embeddings, provedores

logger = logging.getLogger(__name__)

ESCOPOS = ("tabelas", "campos", "ambos")

# O cosseno manda; o lexical desempata e resgata a busca por nome exato.
PESO_COS = 0.85
PESO_LEX = 0.15

# Campo que casa promove a tabela dele, mas vale um pouco menos do que a
# descrição da própria tabela ter casado.
PESO_EVIDENCIA = 0.9

# Quantos campos mostrar como evidência de cada tabela, no escopo "ambos"
EVIDENCIAS = 3

# Token menor que isto é ruído lexical ("de", "da", "id")
MINIMO_TOKEN = 3

# Piso de relevância (0..100) do que é devolvido ao agente. Abaixo disto a
# linha é ruído reescalado -- ver o cabeçalho: a tela podia mostrar, o agente
# não pode receber. Vale como default; a tool aceita outro valor.
MINIMO_RELEVANCIA = 12

# Similaridade média entre vetores sem relação, POR MODELO -- é o zero da escala
# de relevância. Trocar de modelo EXIGE remedir com a consulta do topo: a linha
# de base é própria de cada modelo, não do catálogo.
LINHA_DE_BASE = {"qwen/qwen3-embedding-8b": 0.456}


# --------------------------------------------------------------------------
# Ranking (puro -- é o que os testes cobrem)
# --------------------------------------------------------------------------


def sem_acento(texto):
    """"internação" -> "INTERNACAO". Os nomes vindos do Oracle são ASCII maiúsculo,
    e o Postgres daqui não tem a extensão unaccent -- então quem se ajusta é o termo."""
    base = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return base.upper()


def escapar_like(texto):
    """Neutraliza os curingas de LIKE. O escape padrão do Postgres é a barra.

    Sem isto o `_` do termo vira curinga: "NM_PACIENTE" casaria NMXPACIENTE, e
    o prefixo excluído "BKP_" levaria junto BKPA e BKPX. Nomes de Oracle são
    cheios de `_`, então isto acontece na busca comum, não num caso de borda.
    """
    return texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def tokens(termo):
    """Palavras úteis do termo, normalizadas, para o casamento lexical."""
    return [t for t in sem_acento(termo).split() if len(t) >= MINIMO_TOKEN]


def tokens_like(termo):
    """Os mesmos tokens, prontos para entrar num LIKE.

    Só o lado SQL escapa: lexical() aqui embaixo usa `in`, que já é literal.
    """
    return [escapar_like(t) for t in tokens(termo)]


def prefixos(texto):
    """"bkp_, x" -> ["BKP\\_%", "X%"], os padrões LIKE do que sai da busca."""
    padroes = []
    for bruto in (texto or "").split(","):
        prefixo = sem_acento(bruto).strip()
        if prefixo:
            padroes.append(escapar_like(prefixo) + "%")
    return padroes


def lexical(nome, termo):
    """Quanto o nome do objeto/campo casa com o texto digitado, de 0 a 1.

    Nome exato vale tudo. Fora isso vale a fração dos tokens do termo que
    aparecem no nome -- é o que faz a fórmula servir tanto para "TDPACIENTE"
    quanto para "cadastro de pacientes internados".
    """
    nome, alvo = sem_acento(nome), sem_acento(termo)
    if alvo and nome == alvo:
        return 1.0
    palavras = tokens(termo)
    if not palavras:
        return 0.0
    casados = sum(1 for t in palavras if t in nome)
    return 0.6 * casados / len(palavras)


def score(cos, nome, termo):
    """A nota que ordena a lista. É dela que a relevância exibida sai."""
    return PESO_COS * cos + PESO_LEX * lexical(nome, termo)


def relevancia(score_bruto, modelo):
    """score -> 0..100, com o ruído do modelo puxado para o zero.

    Recebe o SCORE, não o cosseno, de propósito: o número devolvido tem de ser
    monótono com a ordem das linhas. Expor a relevância do cosseno enquanto a
    lista ordena por score deixava 45 acima de 60 -- o resultado parecia quebrado.

    O zero da escala é o ruído puro: cosseno na linha de base e lexical zerado,
    que dá PESO_COS * base. Modelo ainda não medido cai em base 0 e mostra o
    score cru -- melhor um número sem calibragem do que um calibrado com a base
    de outro modelo.
    """
    piso = PESO_COS * LINHA_DE_BASE.get(modelo, 0.0)
    return max(0.0, min(100.0, 100 * (score_bruto - piso) / (1 - piso)))


# --------------------------------------------------------------------------
# Consultas
#
# WHERE embedding_modelo = %(modelo)s é OBRIGATÓRIO em toda consulta com <=>:
# a coluna guarda vetores de modelos diferentes e o operador não devolve
# resultado ruim quando as dimensões divergem -- ele levanta erro.
# --------------------------------------------------------------------------

# O lexical em SQL: nome exato, senão a fração dos tokens presentes no nome.
# Espelha lexical() acima -- mudou um, muda o outro.
LEXICAL = """
    CASE WHEN upper({nome}) = %(termo)s THEN 1.0 ELSE 0.6 * (
        SELECT count(*)::float FROM unnest(%(tokens)s::text[]) t
         WHERE upper({nome}) LIKE '%%' || t || '%%'
    ) / greatest(1, coalesce(array_length(%(tokens)s::text[], 1), 0)) END
"""

# Prefixos que o usuário mandou fora (BKP_, X...). Com a lista vazia o LIKE ANY
# devolve false e o NOT deixa tudo passar -- não precisa de caso especial.
EXCLUI = "AND NOT (upper({nome}) LIKE ANY (%(excluir)s::text[]))"

SQL_TABELAS = f"""
SELECT tabela, rotulo, dominio, resumo, registros, cos, score FROM (
    SELECT o.nome AS tabela,
           coalesce(d.rotulo, '')  AS rotulo,
           coalesce(d.dominio, '') AS dominio,
           coalesce(d.resumo, '')  AS resumo,
           o.num_registros AS registros,
           1 - (o.embedding <=> %(vetor)s::vector) AS cos,
           {PESO_COS} * (1 - (o.embedding <=> %(vetor)s::vector))
           + {PESO_LEX} * {LEXICAL.format(nome='o.nome')} AS score
      FROM catalogo.objeto o
      LEFT JOIN catalogo.descricao_ia d ON d.objeto_id = o.id
     WHERE o.embedding_modelo = %(modelo)s
       {EXCLUI.format(nome='o.nome')}
) r ORDER BY score DESC LIMIT %(topk)s
"""

# O casamento é do campo, mas quem lê precisa situar a TABELA dele: uma coluna
# perfeita numa tabela de 0 registros não serve para consultar nada. `registros`
# sai do JOIN que já existe aqui -- não custa consulta nem join a mais.
SQL_CAMPOS = f"""
SELECT tabela, campo, descricao, registros, cos, score FROM (
    SELECT o.nome AS tabela,
           c.nome AS campo,
           coalesce(c.descricao_ia, '') AS descricao,
           o.num_registros AS registros,
           1 - (c.embedding <=> %(vetor)s::vector) AS cos,
           {PESO_COS} * (1 - (c.embedding <=> %(vetor)s::vector))
           + {PESO_LEX} * {LEXICAL.format(nome='c.nome')} AS score
      FROM catalogo.coluna c
      JOIN catalogo.objeto o ON o.id = c.objeto_id
     WHERE c.embedding_modelo = %(modelo)s
       {EXCLUI.format(nome='o.nome')}
) r ORDER BY score DESC LIMIT %(topk)s
"""

# Tabelas no topo, campo que casou como evidência. O campo promove a tabela dele
# (por isso o greatest), e o LEFT JOIN deixa entrar tabela que só aparece porque
# um campo dela casou -- ela mesma pode nem ter vetor.
SQL_AMBOS = f"""
WITH campo AS (
    SELECT c.objeto_id, c.nome,
           {PESO_COS} * (1 - (c.embedding <=> %(vetor)s::vector))
           + {PESO_LEX} * {LEXICAL.format(nome='c.nome')} AS score,
           1 - (c.embedding <=> %(vetor)s::vector)        AS cos
      FROM catalogo.coluna c
     WHERE c.embedding_modelo = %(modelo)s
),
tabela AS (
    SELECT o.id,
           {PESO_COS} * (1 - (o.embedding <=> %(vetor)s::vector))
           + {PESO_LEX} * {LEXICAL.format(nome='o.nome')} AS score,
           1 - (o.embedding <=> %(vetor)s::vector)        AS cos
      FROM catalogo.objeto o
     WHERE o.embedding_modelo = %(modelo)s
),
melhor AS (
    SELECT objeto_id, max(score) AS score FROM campo GROUP BY objeto_id
)
SELECT o.nome AS tabela,
       coalesce(d.rotulo, '')  AS rotulo,
       coalesce(d.dominio, '') AS dominio,
       coalesce(d.resumo, '')  AS resumo,
       o.num_registros AS registros,
       greatest(coalesce(t.score, 0), {PESO_EVIDENCIA} * coalesce(m.score, 0)) AS score,
       coalesce(t.cos, 0) AS cos,
       ev.campos, ev.campos_score
  FROM catalogo.objeto o
  LEFT JOIN tabela t ON t.id = o.id
  LEFT JOIN melhor m ON m.objeto_id = o.id
  LEFT JOIN catalogo.descricao_ia d ON d.objeto_id = o.id
  LEFT JOIN LATERAL (
      SELECT array_agg(x.nome  ORDER BY x.score DESC) AS campos,
             array_agg(x.score ORDER BY x.score DESC) AS campos_score
        FROM (SELECT nome, score FROM campo
               WHERE objeto_id = o.id ORDER BY score DESC LIMIT {EVIDENCIAS}) x
  ) ev ON true
 WHERE (t.id IS NOT NULL OR m.objeto_id IS NOT NULL)
   {EXCLUI.format(nome='o.nome')}
 ORDER BY score DESC
 LIMIT %(topk)s
"""

CONSULTA = {"tabelas": SQL_TABELAS, "campos": SQL_CAMPOS, "ambos": SQL_AMBOS}


# --------------------------------------------------------------------------
# Cobertura: em que espaço vetorial dá para buscar
# --------------------------------------------------------------------------

SQL_COBERTURA = """
-- O ::int não é enfeite: sum() devolve numeric e sairia "230,00" no JSON.
SELECT modelo,
       sum(objetos)::int AS tabelas,
       sum(colunas)::int AS campos
  FROM (SELECT embedding_modelo AS modelo, count(*) AS objetos, 0 AS colunas
          FROM catalogo.objeto WHERE embedding IS NOT NULL GROUP BY embedding_modelo
         UNION ALL
        SELECT embedding_modelo, 0, count(*)
          FROM catalogo.coluna WHERE embedding IS NOT NULL GROUP BY embedding_modelo) t
 GROUP BY modelo ORDER BY modelo
"""


def cobertura():
    """Quantas tabelas e campos existem em cada modelo. Não chama API nenhuma."""
    return catalogo.consultar(SQL_COBERTURA, (), "cobertura dos vetores")


class SemCobertura(Exception):
    """O modelo do provedor não é o que vetorizou o catálogo.

    Sem isto a busca seria um sucesso silencioso com zero linhas: o
    `WHERE embedding_modelo = %(modelo)s` não casa nada e o SELECT devolve
    vazio, sem erro. Um agente leria "não existe tabela sobre isso" e responderia
    a pergunta errada com confiança.
    """

    def __init__(self, modelo, disponiveis):
        self.modelo = modelo
        self.disponiveis = disponiveis
        super().__init__(
            f"o catálogo não tem vetores do modelo '{modelo}'; "
            f"vetorizado com: {', '.join(disponiveis) or 'nenhum modelo'}"
        )


# --------------------------------------------------------------------------
# Entrada pública
# --------------------------------------------------------------------------


def buscar(termo, escopo="ambos", topk=20, par=None, excluir="",
           min_relevancia=MINIMO_RELEVANCIA):
    """Devolve (linhas, meta). Uma chamada de embedding por termo novo.

    Cada linha traz `relevancia` (0..100); `cos` e `score` ficam no log, não no
    retorno -- quem consome é um agente, e ranking cru só ocupa contexto.
    """
    escopo = escopo if escopo in ESCOPOS else "ambos"
    prov = provedores.escolher(par, capacidade="embedding")

    modelos = [linha["modelo"] for linha in cobertura()]
    if prov.modelo not in modelos:
        raise SemCobertura(prov.modelo, modelos)

    parametros = {
        "vetor": embeddings.vetor_da_consulta(termo, prov),
        "modelo": prov.modelo,
        "termo": sem_acento(termo),
        "tokens": tokens_like(termo),
        "excluir": prefixos(excluir),
        "topk": topk,
    }

    inicio = time.monotonic()
    linhas = catalogo.consultar(
        CONSULTA[escopo], parametros, f"busca semântica em {escopo}"
    )
    decorrido = time.monotonic() - inicio

    resultado = []
    for linha in linhas:
        nota = relevancia(linha["score"], prov.modelo)
        logger.debug("%s: cos=%.3f score=%.3f rel=%.1f",
                     linha.get("tabela"), linha.get("cos", 0), linha["score"], nota)
        if nota < min_relevancia:
            continue
        # "NM_PACIENTE (68) · DT_NASC (54)": o campo que puxou a tabela para cima
        if linha.get("campos"):
            linha["evidencia"] = " · ".join(
                f"{nome} ({relevancia(s, prov.modelo):.0f})"
                for nome, s in zip(linha["campos"], linha["campos_score"])
            )
        for interno in ("cos", "score", "campos", "campos_score"):
            linha.pop(interno, None)
        linha["relevancia"] = round(nota, 1)
        resultado.append(linha)

    logger.info(
        "busca '%s' (%s, top %s, exclui %s, piso %s) via %s: %s de %s linhas em %.0f ms",
        termo, escopo, topk, parametros["excluir"] or "nada", min_relevancia,
        prov.modelo, len(resultado), len(linhas), decorrido * 1000,
    )
    meta = {
        "modelo": prov.modelo,
        "escopo": escopo,
        "resultados": len(resultado),
        "descartados_por_relevancia": len(linhas) - len(resultado),
        "ms": round(decorrido * 1000),
    }
    return resultado, meta
