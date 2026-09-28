"""Listagem de tabelas do catálogo por nome ou rótulo, com o mapa de FKs.

Este módulo é a CONSULTA; src/main.py expõe a tool. É o terceiro caminho de
entrada, ao lado dos outros dois -- e a diferença entre eles é o que se sabe:

- src/busca.py  : não sei o nome, sei o assunto. Vetor, custa embedding.
- src/listagem.py: sei (parte d)o nome ou do rótulo. LIKE, não custa nada.
- src/objeto.py : sei a tabela, quero tudo dela.

Por que LIKE e não mais uma busca vetorial: quem digita "PRESCRIP" quer as
tabelas cujo nome contém aquilo, todas, na ordem do nome -- não as 20 mais
parecidas com o conceito de prescrição. São perguntas diferentes, e o cosseno
responde mal a primeira: ele nunca devolve "todas", só as mais próximas.

O termo varre nome E rótulo no mesmo parâmetro porque o catálogo é bilíngue por
acidente: a tabela se chama CAPADMISSION e o rótulo diz "Admissões de
pacientes". Obrigar a escolher em qual campo procurar seria repassar ao
chamador um detalhe que é nosso.

Dependência e dependente vêm de catalogo.relacionamento, que é a FK de verdade.
A descricao_ia também guarda as duas listas, e conferido no banco elas têm
exatamente as mesmas tabelas -- o que a IA acrescenta é o `motivo`, em
português. Então a contagem e o filtro saem da FK (factual) e o motivo entra
como enfeite quando existe: uma fonte só decide quem aparece, e a outra explica.
"""

import logging

from . import busca, catalogo

logger = logging.getLogger(__name__)

# Teto de tabelas por chamada. O catálogo tem 2.269 objetos e um LIKE de uma
# letra casa quase todos -- sem teto uma tool de listagem vira um despejo.
LIMITE_MAXIMO = 100
LIMITE_PADRAO = 25

# Colunas por tabela quando `campos` está ligado. Aqui o teto é mais apertado
# que o de describe_catalog_table (200): lá é UMA tabela, aqui são até 100, e
# 100 x 194 colunas seriam ~200 mil tokens numa resposta só.
COLUNAS_POR_TABELA = 30

# O Postgres deste catálogo não tem a extensão unaccent, e o rótulo da IA é
# português acentuado ("Admissões de pacientes"). Sem isto, procurar por
# "admissoes" não acha nada -- e é assim que se digita sem pensar.
SEM_ACENTO_SQL = (
    "translate(upper({campo}), "
    "'ÁÀÃÂÄÉÈÊËÍÌÎÏÓÒÕÔÖÚÙÛÜÇÑ', 'AAAAAEEEEIIIIOOOOOUUUUCN')"
)

NOME_SEM_ACENTO = SEM_ACENTO_SQL.format(campo="o.nome")
ROTULO_SEM_ACENTO = SEM_ACENTO_SQL.format(campo="coalesce(d.rotulo, '')")
CAMPO_SEM_ACENTO = SEM_ACENTO_SQL.format(campo="c.nome")
DESCRICAO_SEM_ACENTO = SEM_ACENTO_SQL.format(campo="coalesce(c.descricao_ia, '')")

# Dependência/dependente de um CAMPO, não de uma tabela.
#
# catalogo.relacionamento guarda a FK como dois arrays paralelos: a posição i de
# origem_colunas casa com a posição i de destino_colunas. Conferido no banco --
# 1.092 relacionamentos, 3 compostos, nenhum com os arrays de tamanhos
# diferentes. Por isso array_position() acha o par certo mesmo na FK composta de
# SECUSERRESOURCE, que liga três colunas de uma vez.
#
# O sentido segue o da tabela: dependência é para onde o campo aponta,
# dependente é quem aponta para ele. Chave primária costuma ter dependente aos
# montes e dependência nenhuma.


class CampoNaoEncontrado(Exception):
    def __init__(self, campo, tabela):
        onde = f" na tabela {tabela}" if tabela else " em nenhuma tabela"
        self.motivo = f"Não existe campo '{campo}'{onde}"
        self.detalhe = (
            "Use list_catalog_fields para localizar o campo pelo nome ou pela "
            "descrição."
        )
        super().__init__(self.motivo)


# As duas contagens de FK saem de LATERAL, não de subselect no SELECT, porque
# elas também entram no WHERE dos filtros -- e no WHERE o alias do LATERAL está
# disponível, o do SELECT não.
FROM_BASE = f"""
  FROM catalogo.objeto o
  LEFT JOIN catalogo.descricao_ia d ON d.objeto_id = o.id
  CROSS JOIN LATERAL (
      SELECT count(*)::int AS n FROM catalogo.relacionamento r
       WHERE r.origem_tabela = o.nome) dep
  CROSS JOIN LATERAL (
      SELECT count(*)::int AS n FROM catalogo.relacionamento r
       WHERE r.destino_tabela = o.nome) dpt
 WHERE ({NOME_SEM_ACENTO} LIKE %(padrao)s
        OR {ROTULO_SEM_ACENTO} LIKE %(padrao)s)
"""

SQL_LISTA = f"""
SELECT o.id,
       o.nome AS tabela,
       coalesce(d.rotulo, '')  AS rotulo,
       coalesce(d.dominio, '') AS dominio,
       o.num_registros AS registros,
       o.tem_registros,
       (SELECT count(*)::int FROM catalogo.coluna c WHERE c.objeto_id = o.id) AS n_colunas,
       dep.n AS fks_saindo,
       dpt.n AS fks_entrando
{FROM_BASE}
   {{filtros}}
 ORDER BY o.nome
 LIMIT %(limite)s
"""

# Quantas casaram ANTES do LIMIT. Sem este número, 25 linhas de volta podem ser
# o resultado inteiro ou a ponta de 400 -- e o agente não tem como saber qual.
SQL_TOTAL = f"""
SELECT count(*)::int AS total
{FROM_BASE}
   {{filtros}}
"""

# Uma consulta para o lote todo, não uma por tabela: são até 100 nomes.
SQL_RELACIONAMENTOS = """
SELECT {origem} AS tabela,
       {destino} AS alvo,
       count(*)::int AS fks,
       array_agg(DISTINCT array_to_string(r.origem_colunas, ',')) AS colunas
  FROM catalogo.relacionamento r
 WHERE {origem} = ANY(%(nomes)s)
 GROUP BY 1, 2
 ORDER BY 1, 3 DESC, 2
"""

# O `motivo` que a IA escreveu para cada ligação, achatado do jsonb.
SQL_MOTIVOS = """
SELECT o.nome AS tabela, x.tabela AS alvo, x.motivo
  FROM catalogo.descricao_ia d
  JOIN catalogo.objeto o ON o.id = d.objeto_id
  CROSS JOIN LATERAL jsonb_to_recordset(d.{campo})
       AS x(tabela text, motivo text)
 WHERE o.nome = ANY(%(nomes)s)
"""

SQL_CAMPOS = """
SELECT objeto_id, posicao, nome, tipo_completo, aceita_nulo, eh_pk, descricao_ia
  FROM catalogo.coluna
 WHERE objeto_id = ANY(%(ids)s)
 ORDER BY objeto_id, posicao
"""


def _filtros(tem_dependencias, tem_dependentes, max_n_registros):
    """Os filtros opcionais. None em qualquer um não filtra nada.

    O de registros exige `num_registros IS NOT NULL`: 40 tabelas do catálogo
    estão com a contagem em branco, e sem isso o `<=` devolveria NULL para elas
    -- que o WHERE descarta em silêncio. Melhor descartar dizendo por quê.
    """
    partes = []
    if tem_dependencias is not None:
        partes.append("AND dep.n > 0" if tem_dependencias else "AND dep.n = 0")
    if tem_dependentes is not None:
        partes.append("AND dpt.n > 0" if tem_dependentes else "AND dpt.n = 0")
    if max_n_registros is not None:
        partes.append(
            "AND o.num_registros IS NOT NULL "
            "AND o.num_registros <= %(max_n_registros)s"
        )
    return "\n   ".join(partes)


def _ligacoes(nomes, saindo):
    """{tabela: [{tabela, fks, colunas, motivo}]} numa direção.

    `saindo` True = do que a tabela depende; False = quem depende dela.
    """
    origem, destino = ("r.origem_tabela", "r.destino_tabela")
    if not saindo:
        origem, destino = destino, origem

    linhas = catalogo.consultar(
        SQL_RELACIONAMENTOS.format(origem=origem, destino=destino),
        {"nomes": nomes},
        "dependências" if saindo else "dependentes",
    )
    motivos = {
        (m["tabela"], m["alvo"]): m["motivo"]
        for m in catalogo.consultar(
            SQL_MOTIVOS.format(campo="dependencias" if saindo else "dependentes"),
            {"nomes": nomes},
            "motivos da IA",
        )
    }

    por_tabela = {}
    for linha in linhas:
        ligacao = {
            "tabela": linha["alvo"],
            "fks": linha["fks"],
            "colunas": linha["colunas"],
        }
        motivo = motivos.get((linha["tabela"], linha["alvo"]))
        if motivo:
            ligacao["motivo"] = motivo
        por_tabela.setdefault(linha["tabela"], []).append(ligacao)
    return por_tabela


def _campos(ids):
    """{objeto_id: [colunas]}, cortado em COLUNAS_POR_TABELA por tabela."""
    por_objeto = {}
    for linha in catalogo.consultar(SQL_CAMPOS, {"ids": ids}, "campos das tabelas"):
        lista = por_objeto.setdefault(linha["objeto_id"], [])
        if len(lista) >= COLUNAS_POR_TABELA:
            continue
        coluna = {
            "pos": linha["posicao"],
            "nome": linha["nome"],
            "tipo": linha["tipo_completo"],
            "nulo": linha["aceita_nulo"],
            "descricao": linha["descricao_ia"] or "",
        }
        if linha["eh_pk"]:
            coluna["pk"] = True
        lista.append(coluna)
    return por_objeto


def listar(termo="", tem_dependencias=None, tem_dependentes=None,
           max_n_registros=None, dependencias=False, dependentes=False,
           campos=False, limite=LIMITE_PADRAO):
    """Tabelas cujo nome OU rótulo contém `termo`. Não chama API de embedding."""
    alvo = busca.escapar_like(busca.sem_acento(termo))
    parametros = {
        "padrao": f"%{alvo}%",
        "limite": max(1, min(limite, LIMITE_MAXIMO)),
    }
    if max_n_registros is not None:
        parametros["max_n_registros"] = max_n_registros
    filtros = _filtros(tem_dependencias, tem_dependentes, max_n_registros)

    total = catalogo.um(
        SQL_TOTAL.format(filtros=filtros), parametros, "total da listagem"
    )["total"]
    linhas = catalogo.consultar(
        SQL_LISTA.format(filtros=filtros), parametros, "listagem de tabelas"
    )

    resposta = {
        "termo": termo,
        "filtros": {
            "tem_dependencias": tem_dependencias,
            "tem_dependentes": tem_dependentes,
            "max_n_registros": max_n_registros,
        },
        "total_encontrado": total,
        "count": len(linhas),
    }
    if total > len(linhas):
        resposta["aviso"] = (
            f"{total} tabelas casaram e {len(linhas)} vieram, na ordem do nome. "
            "Refine o termo ou suba `limite`."
        )

    nomes = [linha["tabela"] for linha in linhas]
    ids = [linha["id"] for linha in linhas]
    por_dependencia = _ligacoes(nomes, saindo=True) if (dependencias and nomes) else {}
    por_dependente = _ligacoes(nomes, saindo=False) if (dependentes and nomes) else {}
    por_campo = _campos(ids) if (campos and ids) else {}

    for linha in linhas:
        oid, nome = linha.pop("id"), linha["tabela"]
        if dependencias:
            linha["dependencias"] = por_dependencia.get(nome, [])
        if dependentes:
            linha["dependentes"] = por_dependente.get(nome, [])
        if campos:
            linha["campos"] = por_campo.get(oid, [])
            faltam = linha["n_colunas"] - len(linha["campos"])
            if faltam > 0:
                linha["campos_omitidos"] = faltam

    resposta["rows"] = linhas
    logger.info(
        "listagem '%s' (dep=%s dpt=%s max_reg=%s): %s de %s tabelas",
        termo, tem_dependencias, tem_dependentes, max_n_registros,
        len(linhas), total,
    )
    return resposta


# --------------------------------------------------------------------------
# Campos
# --------------------------------------------------------------------------

# O filtro e o LIMIT acontecem no subselect; as duas contagens de FK ficam
# FORA dele. São 52.975 colunas e 1.092 relacionamentos -- contar antes do corte
# faria o cruzamento para o catálogo inteiro a cada busca. Aqui só as `limite`
# linhas que sobreviveram pagam a conta.
SQL_CAMPOS_LISTA = f"""
SELECT r.*,
       dep.n AS dependencias,
       dpt.n AS dependentes
  FROM (
    SELECT c.id,
           o.nome AS tabela,
           coalesce(d.rotulo, '') AS tabela_rotulo,
           o.num_registros        AS tabela_registros,
           c.posicao, c.nome AS campo, c.tipo, c.tipo_completo,
           c.tamanho, c.precisao, c.escala,
           c.aceita_nulo, c.valor_default, c.comentario, c.eh_pk, c.na_amostra,
           coalesce(c.descricao_ia, '') AS descricao
      FROM catalogo.coluna c
      JOIN catalogo.objeto o ON o.id = c.objeto_id
      LEFT JOIN catalogo.descricao_ia d ON d.objeto_id = o.id
     WHERE ({CAMPO_SEM_ACENTO} LIKE %(padrao)s
            OR {DESCRICAO_SEM_ACENTO} LIKE %(padrao)s)
     ORDER BY o.nome, c.posicao
     LIMIT %(limite)s
  ) r
  CROSS JOIN LATERAL (
      SELECT count(*)::int AS n FROM catalogo.relacionamento fk
       WHERE fk.origem_tabela = r.tabela
         AND r.campo = ANY(fk.origem_colunas)) dep
  CROSS JOIN LATERAL (
      SELECT count(*)::int AS n FROM catalogo.relacionamento fk
       WHERE fk.destino_tabela = r.tabela
         AND r.campo = ANY(fk.destino_colunas)) dpt
 ORDER BY r.tabela, r.posicao
"""

SQL_CAMPOS_TOTAL = f"""
SELECT count(*)::int AS total
  FROM catalogo.coluna c
 WHERE ({CAMPO_SEM_ACENTO} LIKE %(padrao)s
        OR {DESCRICAO_SEM_ACENTO} LIKE %(padrao)s)
"""

# Nulo em coluna numérica quer dizer "não se aplica" (tamanho de um NUMBER sem
# precisão declarada, por exemplo). Sai da linha em vez de virar `null`.
OPCIONAIS_DO_CAMPO = ("tamanho", "precisao", "escala", "valor_default", "comentario")


def _limpar_campo(linha):
    linha.pop("id", None)
    for chave in OPCIONAIS_DO_CAMPO:
        if linha.get(chave) in (None, ""):
            linha.pop(chave, None)
    return linha


def listar_campos(termo="", limite=LIMITE_PADRAO):
    """Campos cujo nome OU descrição contém `termo`. Não chama API de embedding."""
    alvo = busca.escapar_like(busca.sem_acento(termo))
    parametros = {
        "padrao": f"%{alvo}%",
        "limite": max(1, min(limite, LIMITE_MAXIMO)),
    }

    total = catalogo.um(SQL_CAMPOS_TOTAL, parametros, "total de campos")["total"]
    linhas = catalogo.consultar(SQL_CAMPOS_LISTA, parametros, "listagem de campos")

    resposta = {
        "termo": termo,
        "total_encontrado": total,
        "count": len(linhas),
        "rows": [_limpar_campo(linha) for linha in linhas],
    }
    if total > len(linhas):
        resposta["aviso"] = (
            f"{total} campos casaram e {len(linhas)} vieram, na ordem de tabela e "
            "posição. Refine o termo ou suba `limite`."
        )
    logger.info("listagem de campos '%s': %s de %s", termo, len(linhas), total)
    return resposta


# --------------------------------------------------------------------------
# Dependências e dependentes de um campo
# --------------------------------------------------------------------------

# destino_colunas[array_position(origem_colunas, campo)] devolve a coluna
# emparelhada do outro lado -- na FK simples é a única, na composta é a que
# ocupa a mesma posição.
SQL_CAMPO_DEPENDENCIAS = """
SELECT r.origem_tabela AS tabela,
       r.constraint_nome,
       r.destino_tabela AS aponta_para_tabela,
       r.destino_colunas[array_position(r.origem_colunas, %(campo)s)]
           AS aponta_para_campo,
       r.origem_colunas AS colunas_da_fk,
       r.delete_rule
  FROM catalogo.relacionamento r
 WHERE %(campo)s = ANY(r.origem_colunas)
   {filtro_tabela}
 ORDER BY r.origem_tabela, r.destino_tabela
"""

SQL_CAMPO_DEPENDENTES = """
SELECT r.destino_tabela AS tabela,
       r.constraint_nome,
       r.origem_tabela AS apontado_por_tabela,
       r.origem_colunas[array_position(r.destino_colunas, %(campo)s)]
           AS apontado_por_campo,
       r.origem_colunas AS colunas_da_fk,
       r.delete_rule
  FROM catalogo.relacionamento r
 WHERE %(campo)s = ANY(r.destino_colunas)
   {filtro_tabela}
 ORDER BY r.destino_tabela, r.origem_tabela
"""

# Em quantas tabelas o campo existe, sem o corte do limite. `aparece_em` mostra
# as `limite` primeiras; anunciar o tamanho da lista como se fosse o total faria
# "IDPATIENT existe em 5 tabelas" quando são 48.
SQL_ONDE_APARECE_TOTAL = """
SELECT count(*)::int AS total
  FROM catalogo.coluna c
  JOIN catalogo.objeto o ON o.id = c.objeto_id
 WHERE upper(c.nome) = upper(%(campo)s)
   {filtro_tabela}
"""

SQL_ONDE_APARECE = """
SELECT o.nome AS tabela, c.posicao, c.tipo_completo, c.eh_pk,
       coalesce(c.descricao_ia, '') AS descricao,
       o.num_registros AS tabela_registros
  FROM catalogo.coluna c
  JOIN catalogo.objeto o ON o.id = c.objeto_id
 WHERE upper(c.nome) = upper(%(campo)s)
   {filtro_tabela}
 ORDER BY o.nome
 LIMIT %(limite)s
"""


def relacoes_do_campo(campo, tabela="", limite=LIMITE_PADRAO):
    """FKs que saem e que entram numa coluna.

    `tabela` vazia responde a pergunta mais ampla -- "quem depende de IDPATIENT,
    em qualquer tabela" -- que é o caso em que o nome da coluna se repete pelo
    schema inteiro. Com a tabela informada, o recorte é o campo daquela tabela.
    """
    campo = (campo or "").strip().upper()
    if not campo:
        raise ValueError("informe o nome do campo")

    parametros = {"campo": campo, "limite": max(1, min(limite, LIMITE_MAXIMO))}
    filtro_rel, filtro_col = "", ""
    if tabela:
        parametros["tabela"] = tabela.strip().upper()
        filtro_col = "AND upper(o.nome) = %(tabela)s"

    total_onde = catalogo.um(
        SQL_ONDE_APARECE_TOTAL.format(filtro_tabela=filtro_col), parametros,
        f"em quantas tabelas {campo} aparece",
    )["total"]
    onde = catalogo.consultar(
        SQL_ONDE_APARECE.format(filtro_tabela=filtro_col), parametros,
        f"onde o campo {campo} aparece",
    )
    if not onde:
        raise CampoNaoEncontrado(campo, tabela)

    if tabela:
        filtro_rel = "AND upper(r.origem_tabela) = %(tabela)s"
    dependencias = catalogo.consultar(
        SQL_CAMPO_DEPENDENCIAS.format(filtro_tabela=filtro_rel), parametros,
        f"dependências de {campo}",
    )
    if tabela:
        filtro_rel = "AND upper(r.destino_tabela) = %(tabela)s"
    dependentes = catalogo.consultar(
        SQL_CAMPO_DEPENDENTES.format(filtro_tabela=filtro_rel), parametros,
        f"dependentes de {campo}",
    )

    logger.info(
        "relações de %s%s: %s dependências, %s dependentes, em %s tabelas",
        campo, f" (tabela {tabela})" if tabela else "",
        len(dependencias), len(dependentes), len(onde),
    )
    resposta = {
        "campo": campo,
        "tabela": tabela.strip().upper() if tabela else None,
        "aparece_em": onde,
        "dependencias": dependencias,
        "dependentes": dependentes,
        "count": {
            "tabelas_com_o_campo": total_onde,
            "tabelas_listadas": len(onde),
            "dependencias": len(dependencias),
            "dependentes": len(dependentes),
        },
    }
    if total_onde > len(onde):
        resposta["aviso"] = (
            f"O campo existe em {total_onde} tabelas e {len(onde)} vieram em "
            "`aparece_em`. As FKs abaixo cobrem todas, não só as listadas."
        )
    return resposta
