"""Detalhe de um objeto do catálogo: o que a página /oracle/objetos/<NOME> mostra.

Este módulo é a CONSULTA; src/main.py expõe a tool -- a mesma divisão de
src/busca.py. A origem é ~/apps/opalus-data-connect/src/oracle.py
(`gerar_detalhe`), que monta o HTML; aqui o mesmo conteúdo vira payload.

O consumidor mudou de página para agente, e o tamanho passou a ser o problema
central. Medido em CAPADMISSION (194 colunas, 31.560 registros):

    coluna com o vetor .............. 9.970 kB   <- nunca
    coluna sem o vetor ................. 32 kB
    amostra (20 linhas) ................ 93 kB
    descricao_ia com `campos` .......... 25 kB
    restrições .......................... 8 kB
    índices ............................. 2 kB

Uma página rola; um contexto não. Devolver a página inteira seriam ~40 mil
tokens numa chamada -- três tabelas e o agente não tem mais espaço para pensar.
Daí as três decisões deste módulo:

1. `embedding` nunca sai. São 10 MB dos 10,1 MB da tabela `coluna`, e não há
   nada que um agente faça com 4096 floats.
2. A amostra é opt-in e vem com os nulos podados. São 194 chaves por linha, a
   maioria nula: o que interessa é o formato do dado que EXISTE.
3. `descricao_ia.campos` não entra: é a descrição de cada coluna, que já vai
   anexada à coluna correspondente em `colunas`.

E uma diferença de conteúdo, não de tamanho: a página mostra as restrições e o
grafo de vizinhança lado a lado, e as FKs de saída aparecem nos dois. Aqui as
FKs saindo estão só em `chaves`, com detalhe de coluna, e `referenciada_por`
guarda o que a página só tinha no desenho -- quem aponta para cá.
"""

import logging
import unicodedata

from . import catalogo

logger = logging.getLogger(__name__)

# Linhas de amostra dentro do detalhe. A página mostra 10; aqui o padrão é não
# mandar nenhuma, e quem pede junto do dicionário raramente precisa de mais de
# três para ver o formato.
AMOSTRA_MAXIMA_NO_DETALHE = 10

# Teto da tool dedicada. A extração coletou 20 linhas por tabela, então pedir
# mais que isso não traz nada -- o teto é o tamanho do que existe.
AMOSTRA_MAXIMA = 20

# Tipos que a extração nunca coletou: o valor não cabe (ou não vira) JSON.
# Espelha TIPOS_FORA_DA_AMOSTRA de mapear_oracle.py.
TIPOS_FORA_DA_AMOSTRA = ("LONG", "LONG RAW", "BLOB", "BFILE", "XMLTYPE", "ANYDATA")

# Teto de colunas devolvidas de uma vez. A mediana do catálogo é 8 colunas e o
# p90 é 43 -- o teto não encosta em 99% das tabelas. Ele existe pelas 27 que
# passam de 200 colunas, uma delas com 1.000: essa sozinha seriam ~38 mil tokens
# numa resposta, e o agente perderia o contexto no meio do dicionário. Truncar
# em silêncio seria pior do que não devolver, então `colunas_omitidas` diz
# quantas ficaram de fora e a tool manda usar `filtro_coluna`.
COLUNAS_MAXIMAS = 200

# Tipos de restrição do dicionário do Oracle
TIPO_RESTRICAO = {"P": "primaria", "U": "unica", "R": "estrangeira"}


class ObjetoNaoEncontrado(Exception):
    def __init__(self, nome, parecidos):
        self.motivo = f"Não existe tabela '{nome}' no catálogo"
        self.detalhe = (
            f"Nomes parecidos: {', '.join(parecidos)}."
            if parecidos
            else "Use search_catalog para localizar a tabela pelo assunto."
        )
        super().__init__(self.motivo)


# --------------------------------------------------------------------------
# Consultas
# --------------------------------------------------------------------------

# upper() no lugar da igualdade direta: o índice único de `nome` fica de fora,
# mas são 2.269 linhas -- e o agente escreve "capadmission" tanto quanto
# "CAPADMISSION".
SQL_OBJETO = """
SELECT o.id, o.nome, o.tipo, o.comentario, o.num_registros, o.contagem_aproximada,
       o.tem_registros, o.particionada, o.view_sql, o.atualizado_em,
       o.embedding_modelo,
       (SELECT count(*) FROM catalogo.coluna c WHERE c.objeto_id = o.id)   AS n_colunas,
       (SELECT count(*) FROM catalogo.restricao r WHERE r.objeto_id = o.id) AS n_restricoes,
       (SELECT count(*) FROM catalogo.indice i WHERE i.objeto_id = o.id)   AS n_indices,
       (SELECT count(*) FROM catalogo.amostra a WHERE a.objeto_id = o.id)  AS n_amostra,
       (SELECT count(*) FROM catalogo.relacionamento r
         WHERE r.origem_tabela = o.nome)                                   AS fks_saindo,
       (SELECT count(*) FROM catalogo.relacionamento r
         WHERE r.destino_tabela = o.nome)                                  AS fks_entrando
  FROM catalogo.objeto o
 WHERE upper(o.nome) = upper(%s)
"""

SQL_PARECIDOS = """
SELECT nome FROM catalogo.objeto
 WHERE nome ILIKE '%%' || %s || '%%' ORDER BY nome LIMIT 5
"""

# `campos` fica de fora de propósito -- ver o cabeçalho.
SQL_DESCRICAO = """
SELECT rotulo, dominio, resumo, funcao, modelo, provedor, gerado_em,
       dependencias, dependentes
  FROM catalogo.descricao_ia WHERE objeto_id = %s
"""

SQL_COLUNAS = """
SELECT posicao, nome, tipo_completo, aceita_nulo, eh_pk, valor_default,
       comentario, descricao_ia
  FROM catalogo.coluna WHERE objeto_id = %s ORDER BY posicao
"""

SQL_RESTRICOES = """
SELECT nome, tipo, colunas, ref_tabela, ref_colunas, delete_rule, status
  FROM catalogo.restricao WHERE objeto_id = %s ORDER BY tipo, nome
"""

SQL_INDICES = """
SELECT nome, unico, tipo, colunas
  FROM catalogo.indice WHERE objeto_id = %s ORDER BY nome
"""

# Quem aponta para cá, agrupado por tabela: 39 FKs entrando viram poucas linhas.
SQL_REFERENCIADA_POR = """
SELECT origem_tabela AS tabela,
       count(*)::int  AS fks,
       array_agg(DISTINCT array_to_string(origem_colunas, ',')) AS colunas,
       coalesce(o.tem_registros, false) AS tem_registros
  FROM catalogo.relacionamento r
  LEFT JOIN catalogo.objeto o ON o.nome = r.origem_tabela
 WHERE r.destino_tabela = %s
 GROUP BY origem_tabela, o.tem_registros
 ORDER BY count(*) DESC, origem_tabela
"""

SQL_AMOSTRA = """
SELECT linha, mascarada, dados
  FROM catalogo.amostra WHERE objeto_id = %s ORDER BY linha LIMIT %s
"""


# --------------------------------------------------------------------------
# Montagem
# --------------------------------------------------------------------------


def _quando(valor):
    """timestamptz -> ISO 8601. O encoder de JSON do MCP não conhece datetime."""
    return valor.isoformat() if valor is not None else None


def _sem_acento(texto):
    base = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return base.upper()


def _casa(coluna, filtro):
    """O filtro varre nome, comentário e descrição -- não só o nome.

    Quem procura "paciente" numa tabela de 194 colunas quer IDPATIENT junto, e
    o nome dele não tem 'paciente' em lugar nenhum: a descrição é que tem.
    """
    alvo = " ".join(str(coluna.get(campo) or "")
                    for campo in ("nome", "comentario", "descricao_ia"))
    return filtro in _sem_acento(alvo)


def _fks_por_coluna(restricoes):
    """{"IDPATIENT": "CAPPATIENT.ID"} -- para anotar a coluna com o destino.

    É a informação que decide um JOIN, e na página ela só existe na tabela de
    restrições, longe da linha da coluna.
    """
    destino = {}
    for r in restricoes:
        if r["tipo"] != "R" or not r["ref_tabela"]:
            continue
        for i, coluna in enumerate(r["colunas"]):
            ref = r["ref_colunas"] or []
            alvo = ref[i] if i < len(ref) else (ref[0] if ref else "")
            destino[coluna] = f"{r['ref_tabela']}.{alvo}" if alvo else r["ref_tabela"]
    return destino


def _montar_colunas(linhas, fks, filtro):
    colunas = []
    for linha in linhas:
        if filtro and not _casa(linha, filtro):
            continue
        coluna = {
            "pos": linha["posicao"],
            "nome": linha["nome"],
            "tipo": linha["tipo_completo"],
            "nulo": linha["aceita_nulo"],
            "descricao": linha["descricao_ia"] or "",
        }
        # Chaves só aparecem quando valem algo: `"pk": false` em 193 das 194
        # colunas é puro peso morto no contexto.
        if linha["eh_pk"]:
            coluna["pk"] = True
        if linha["nome"] in fks:
            coluna["fk"] = fks[linha["nome"]]
        if linha["valor_default"]:
            coluna["default"] = linha["valor_default"]
        if linha["comentario"]:
            coluna["comentario"] = linha["comentario"]
        colunas.append(coluna)
    return colunas


def _montar_chaves(restricoes):
    chaves = {"primaria": [], "unicas": [], "estrangeiras": []}
    for r in restricoes:
        tipo = TIPO_RESTRICAO.get(r["tipo"])
        if tipo == "estrangeira":
            chaves["estrangeiras"].append({
                "nome": r["nome"],
                "colunas": r["colunas"],
                "referencia": f"{r['ref_tabela']}({','.join(r['ref_colunas'] or [])})",
                "delete_rule": r["delete_rule"],
            })
        elif tipo == "primaria":
            chaves["primaria"] = r["colunas"]
        elif tipo == "unica":
            chaves["unicas"].append({"nome": r["nome"], "colunas": r["colunas"]})
    return chaves


def _montar_amostra(linhas):
    """Linhas de amostra como estão guardadas -- nulos inclusive.

    Nada é podado aqui de propósito: a linha sai igual ao que a extração gravou.
    Em CAPADMISSION isso são ~157 chaves nulas de 194 por linha, e o payload
    cresce por causa disso; o dia em que incomodar, a poda volta -- por coluna,
    não por linha, senão cada linha vem com um conjunto de chaves diferente.
    """
    return [
        {
            "linha": linha["linha"],
            "mascarada": linha["mascarada"],
            "dados": linha["dados"] or {},
        }
        for linha in linhas
    ]


# --------------------------------------------------------------------------
# Entrada pública
# --------------------------------------------------------------------------


def detalhar(nome, colunas=True, amostra=0, filtro_coluna=""):
    """Tudo que o catálogo sabe sobre uma tabela. Não chama API de embedding."""
    objeto = catalogo.um(SQL_OBJETO, (nome,), f"objeto {nome}")
    if not objeto:
        parecidos = [
            linha["nome"]
            for linha in catalogo.consultar(SQL_PARECIDOS, (nome,), "nomes parecidos")
        ]
        raise ObjetoNaoEncontrado(nome, parecidos)

    oid, real = objeto["id"], objeto["nome"]
    descricao = catalogo.um(SQL_DESCRICAO, (oid,), f"descrição de {real}")
    restricoes = catalogo.consultar(SQL_RESTRICOES, (oid,), f"restrições de {real}")

    detalhe = {
        "tabela": {
            "nome": real,
            "tipo": objeto["tipo"],
            "comentario": objeto["comentario"] or "",
            "num_registros": objeto["num_registros"],
            "contagem_aproximada": objeto["contagem_aproximada"],
            "tem_registros": objeto["tem_registros"],
            "particionada": objeto["particionada"],
            "mapeado_em": _quando(objeto["atualizado_em"]),
        },
        "estrutura": {
            "colunas": objeto["n_colunas"],
            "restricoes": objeto["n_restricoes"],
            "indices": objeto["n_indices"],
            "linhas_de_amostra": objeto["n_amostra"],
            "fks_saindo": objeto["fks_saindo"],
            "fks_entrando": objeto["fks_entrando"],
        },
        "chaves": _montar_chaves(restricoes),
        "indices": catalogo.consultar(SQL_INDICES, (oid,), f"índices de {real}"),
        "referenciada_por": catalogo.consultar(
            SQL_REFERENCIADA_POR, (real,), f"quem referencia {real}"
        ),
    }

    if objeto["view_sql"]:
        detalhe["tabela"]["view_sql"] = objeto["view_sql"]

    if descricao:
        detalhe["descricao"] = {
            "rotulo": descricao["rotulo"],
            "dominio": descricao["dominio"],
            "resumo": descricao["resumo"],
            "funcao": descricao["funcao"],
            "dependencias": descricao["dependencias"],
            "dependentes": descricao["dependentes"],
            "gerada_em": _quando(descricao["gerado_em"]),
            "modelo": descricao["modelo"],
            # A estrutura mudou depois da descrição ter sido escrita: o texto
            # pode não mencionar colunas novas. É o mesmo aviso que a página dá.
            "desatualizada": descricao["gerado_em"] < objeto["atualizado_em"],
        }
    else:
        detalhe["descricao"] = None

    if colunas:
        filtro = _sem_acento(filtro_coluna) if filtro_coluna else ""
        todas = catalogo.consultar(SQL_COLUNAS, (oid,), f"colunas de {real}")
        selecionadas = _montar_colunas(todas, _fks_por_coluna(restricoes), filtro)
        if filtro:
            detalhe["estrutura"]["colunas_filtradas"] = len(selecionadas)
        if len(selecionadas) > COLUNAS_MAXIMAS:
            detalhe["estrutura"]["colunas_omitidas"] = len(selecionadas) - COLUNAS_MAXIMAS
            detalhe["estrutura"]["aviso"] = (
                f"Só as {COLUNAS_MAXIMAS} primeiras colunas (por posição) vieram. "
                "Use filtro_coluna para chegar às demais."
            )
            selecionadas = selecionadas[:COLUNAS_MAXIMAS]
        detalhe["colunas"] = selecionadas

    if amostra:
        linhas = catalogo.consultar(
            SQL_AMOSTRA, (oid, min(amostra, AMOSTRA_MAXIMA_NO_DETALHE)),
            f"amostra de {real}"
        )
        detalhe["amostra"] = _montar_amostra(linhas)

    logger.info(
        "detalhe de %s: %s colunas, %s restrições, %s índices, %s linhas de amostra",
        real, len(detalhe.get("colunas", [])), objeto["n_restricoes"],
        objeto["n_indices"], len(detalhe.get("amostra", [])),
    )
    return detalhe


# --------------------------------------------------------------------------
# Amostra de dados
#
# A amostra NÃO é uma consulta à origem: é o retrato que a extração guardou em
# catalogo.amostra. Isso decide tudo o que vem abaixo -- ela é datada, tem no
# máximo 20 linhas, já vem mascarada e não aceita WHERE. Devolver esses limites
# junto com os dados não é enfeite: um agente que receba 5 linhas sem saber
# disso vai tratá-las como o estado atual da tabela e concluir, de uma amostra
# de 5 numa tabela de 31.560, que "STATUS é sempre 4".
# --------------------------------------------------------------------------

SQL_EXTRACAO = """
SELECT schema_origem, service_name, concluida_em, parametros
  FROM catalogo.extracao WHERE id = 1
"""

SQL_COLUNAS_DA_AMOSTRA = """
SELECT nome, tipo, posicao FROM catalogo.coluna
 WHERE objeto_id = %s ORDER BY posicao
"""


def _procuradas(colunas):
    """"ID, status ,DIAGNOSTIC" -> {"ID", "STATUS", "DIAGNOSTIC"}."""
    return {parte.strip().upper() for parte in (colunas or "").split(",") if parte.strip()}


def _ordenar(dados, ordem):
    """Reordena a linha pela posição da coluna na tabela.

    O jsonb do Postgres não guarda a ordem em que as chaves entraram -- ele
    ordena por tamanho e depois alfabeticamente. Cru, o ID aparece no meio da
    linha e a amostra não casa com a lista de `describe_catalog_table`. Ler as
    duas lado a lado é o uso normal, então a ordem aqui é a da tabela.
    """
    return {nome: dados[nome] for nome in ordem if nome in dados}


def amostrar(nome, linhas=5, colunas=""):
    """Linhas de exemplo guardadas para uma tabela. Não consulta a origem."""
    objeto = catalogo.um(SQL_OBJETO, (nome,), f"objeto {nome}")
    if not objeto:
        parecidos = [
            linha["nome"]
            for linha in catalogo.consultar(SQL_PARECIDOS, (nome,), "nomes parecidos")
        ]
        raise ObjetoNaoEncontrado(nome, parecidos)

    oid, real = objeto["id"], objeto["nome"]
    quantas = max(1, min(linhas, AMOSTRA_MAXIMA))
    brutas = catalogo.consultar(SQL_AMOSTRA, (oid, quantas), f"amostra de {real}")

    extracao = catalogo.um(SQL_EXTRACAO, (), "dados da extração") or {}
    parametros = extracao.get("parametros") or {}
    catalogadas = catalogo.consultar(SQL_COLUNAS_DA_AMOSTRA, (oid,), f"colunas de {real}")

    resposta = {
        "tabela": real,
        "origem": {
            "fonte": "catalogo.amostra",
            "schema": extracao.get("schema_origem"),
            "service": extracao.get("service_name"),
            "extraida_em": _quando(extracao.get("concluida_em")),
            "mascara": parametros.get("mascara"),
        },
        "num_registros_na_tabela": objeto["num_registros"],
        "linhas_disponiveis": objeto["n_amostra"],
        "linhas_devolvidas": len(brutas),
        # Colunas que a extração pulou pelo TIPO. Sem este aviso a amostra mente
        # por omissão: GLBHISTORYTEXT tem 2 colunas, uma é LONG, e a linha volta
        # com uma chave só -- como se a tabela fosse essa.
        "colunas_fora_da_amostra": [
            f"{c['nome']} ({c['tipo']})"
            for c in catalogadas if c["tipo"] in TIPOS_FORA_DA_AMOSTRA
        ],
    }

    if not brutas:
        resposta["linhas"] = []
        resposta["aviso"] = (
            "A tabela não tem linhas de amostra guardadas"
            + (" -- ela estava vazia na extração." if not objeto["tem_registros"]
               else ", embora tivesse registros na extração.")
        )
        logger.info("amostra de %s: nenhuma linha guardada", real)
        return resposta

    ordem = [c["nome"] for c in catalogadas]
    procuradas = _procuradas(colunas)
    if procuradas:
        ordem = [n for n in ordem if n in procuradas]
        ausentes = procuradas - set(c["nome"] for c in catalogadas)
        if ausentes:
            resposta["colunas_inexistentes"] = sorted(ausentes)

    # Sem poda e sem anotação de máscara: a linha sai como a extração gravou.
    # A única coisa que muda é a ORDEM das chaves -- ver _ordenar. Coluna nula
    # continua na linha, e é por isso que `colunas` (a projeção) importa em
    # tabela larga: CAPADMISSION traz 194 chaves por linha, a maioria nula.
    montadas = [_ordenar(linha["dados"] or {}, ordem) for linha in brutas]

    resposta["linhas"] = [
        {"linha": bruta["linha"], "dados": montada}
        for bruta, montada in zip(brutas, montadas)
    ]

    logger.info(
        "amostra de %s: %s linhas, %s colunas por linha",
        real, len(montadas), len(montadas[0]) if montadas else 0,
    )
    return resposta
