"""As perguntas de faturamento, glosa e auditoria que já têm resposta conferida.

Este módulo fica entre as duas coisas que o servidor já sabia fazer: o catálogo
(src/busca.py, src/objeto.py) diz ONDE o dado mora, e `run_oracle_query`
(src/oracle.py) executa SQL livre. Aqui estão as consultas de negócio já escritas
e validadas -- o agente escolhe a pergunta, não redige o SQL.

A diferença importa porque estas consultas têm três regras que não se adivinham
lendo o schema, e errar qualquer uma devolve um número plausível e menor que o
real:

1. O valor faturado do item depende do tipo de recurso: imposto (RESOURCETYPE 15)
   usa UNITCOST, o resto usa UNITPRICE. Ver VALOR_ITEM.
2. "Unidade de negócio" são DUAS dimensões diferentes. Faturamento, pagamento e
   glosa se organizam por CAPPAYMENT.IDHOMECARE -> GLBENTERPRISE; orçamento e
   admissão, por CAPADMISSION.IDHEALTHPROVDEP -> GLBHEALTHPROVIDER. Os rótulos
   não são intercambiáveis: 'Premier Brooklin' fatura, 'Premier Brooklin HSP'
   orça.
3. O fim do período é EXCLUSIVO (`< :DT_FIM`). Daí `periodo()` -- quem passa o
   mês nunca precisa saber disso.

Tudo somente leitura, pela mesma porta de src/oracle.py: teto de linhas por
ROWNUM, sessão READ ONLY, e os valores viajam como bind, nunca concatenados.

Origem: documento "OPALUS ERP IW -- QUERIES BASE DE FATURAMENTO / GLOSA /
AUDITORIA", schema DBIWGERIATRICS. Onde este módulo se afasta do texto original,
há comentário dizendo por quê -- e são três lugares só: o TRIM do nome da
unidade, a taxonomia de glosa em CTE e a quantidade glosada no detalhe.
"""

import datetime
import logging

from . import oracle

logger = logging.getLogger(__name__)


class PeriodoInvalido(Exception):
    """Recorte de tempo ausente ou malformado. Não chega a ir ao banco."""

    def __init__(self, motivo, detalhe=""):
        super().__init__(motivo)
        self.motivo = motivo
        self.detalhe = detalhe


# ---------------------------------------------------------------------------
# Período
# ---------------------------------------------------------------------------


def periodo(mes="", dt_ini="", dt_fim=""):
    """Devolve (inicio, fim EXCLUSIVO) como date. Aceita mês ou intervalo.

    `mes='2025-07'` vira (2025-07-01, 2025-08-01). É o caminho normal, e existe
    para tirar da frente a armadilha do fim exclusivo: quem informa 31/07 como
    fim perde o dia 31 inteiro, e o resultado não tem como acusar isso -- sai um
    número menor, plausível, errado.

    Sem período não há default. "O mês passado" sobre faturamento é chute com
    cara de resposta, então aqui é erro.
    """
    mes = (mes or "").strip()
    dt_ini = (dt_ini or "").strip()
    dt_fim = (dt_fim or "").strip()

    if mes and (dt_ini or dt_fim):
        raise PeriodoInvalido(
            "informe o mês OU o intervalo, não os dois",
            "Use mes='2025-07' para um mês fechado, ou dt_ini e dt_fim para um "
            "recorte livre.",
        )

    if mes:
        try:
            ano, numero = mes.split("-")
            inicio = datetime.date(int(ano), int(numero), 1)
        except (ValueError, TypeError) as falha:
            raise PeriodoInvalido(
                f"'{mes}' não é um mês no formato AAAA-MM",
                "Exemplo: mes='2025-07' para julho de 2025.",
            ) from falha
        # Primeiro dia do mês seguinte: o fim exclusivo que a consulta espera.
        fim = (inicio.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        return inicio, fim

    if not dt_ini or not dt_fim:
        raise PeriodoInvalido(
            "período obrigatório",
            "Informe mes='AAAA-MM' (o caminho mais simples) ou o par "
            "dt_ini/dt_fim em AAAA-MM-DD. Lembre que dt_fim é EXCLUSIVO.",
        )

    try:
        inicio = datetime.date.fromisoformat(dt_ini)
        fim = datetime.date.fromisoformat(dt_fim)
    except ValueError as falha:
        raise PeriodoInvalido(
            "data inválida",
            f"dt_ini='{dt_ini}', dt_fim='{dt_fim}'. Use AAAA-MM-DD.",
        ) from falha

    if fim <= inicio:
        raise PeriodoInvalido(
            "o fim do período não é posterior ao início",
            f"dt_ini={inicio}, dt_fim={fim}. O fim é EXCLUSIVO: para julho "
            "inteiro, use dt_fim='2025-08-01' (ou mes='2025-07').",
        )
    return inicio, fim


def _params(inicio, fim, unidade):
    """Binds das consultas com recorte. `unidade` em branco vira NULL = todas."""
    return {
        "DT_INI": inicio,
        "DT_FIM": fim,
        "UNIDADE": (unidade or "").strip() or None,
    }


# ---------------------------------------------------------------------------
# Fragmentos compartilhados
# ---------------------------------------------------------------------------

# A regra do imposto: RESOURCETYPE 15 fatura pelo custo, o resto pelo preço.
# Constante do módulo, interpolada nas seis consultas que faturam -- uma cópia
# por consulta seria uma chance a mais de as cópias divergirem numa manutenção.
VALOR_ITEM = (
    "CASE WHEN i.RESOURCETYPE = 15 THEN i.UNITCOST ELSE i.UNITPRICE END * i.QUANTITY"
)

# TRIM dos dois lados, e não a igualdade direta do documento original: no banco,
# 'PLENO SAUDE - DF  ' e 'PLENO SAUDE - ES  ' têm espaço no fim (17 caracteres
# contra os 16 do nome que aparece em qualquer lista). Com `hc.NAME = :UNIDADE`,
# duas das treze unidades devolvem ZERO linha e nada denuncia o motivo. UPPER
# pela mesma razão: o nome vem de uma lista fechada, então não há risco de casar
# a unidade errada, e 'premier brooklin' funcionar não custa nada.
FILTRO_HOMECARE = (
    "(:UNIDADE IS NULL OR UPPER(TRIM(hc.NAME)) = UPPER(TRIM(:UNIDADE)))"
)
FILTRO_PROVIDER = (
    "(:UNIDADE IS NULL OR UPPER(TRIM(hp.NAME)) = UPPER(TRIM(:UNIDADE)))"
)
# Orçamento agrupado aceita o nome pelas duas dimensões, como no original.
FILTRO_PROVIDER_OU_HOMECARE = (
    "(:UNIDADE IS NULL"
    " OR UPPER(TRIM(hp.NAME)) = UPPER(TRIM(:UNIDADE))"
    " OR UPPER(TRIM(hc.NAME)) = UPPER(TRIM(:UNIDADE)))"
)

# Um CASE só para as duas consultas de conta. No documento original ele aparece
# duas vezes com rótulos LIGEIRAMENTE diferentes -- a query 3 diz 'Baixada
# (write-off)' e 'Exportada (fechada)' onde a 4 diz 'Baixada' e 'Exportada'. As
# condições são idênticas nas duas, então a diferença é de transcrição, não de
# intenção; aqui vale a forma longa. Medido: fora o rótulo, a query 4 devolve as
# mesmas 804 linhas com os mesmos valores.
#
# Unificar importa para quem lê: dois nomes para o mesmo estado, vindos de duas
# tools do mesmo servidor, se parecem com dois estados diferentes.
SITUACAO_CONTA = """CASE
        WHEN {a}.SIMULATION = 1 THEN 'Simulação'
        WHEN {a}.WRITEOFF   = 1 THEN 'Baixada (write-off)'
        WHEN {a}.LIBERATED  = 0 THEN 'Aberta (não liberada)'
        WHEN {a}.EXPORTED   = 0 THEN 'Liberada (não exportada)'
        ELSE 'Exportada (fechada)'
    END"""

SITUACAO_ORCAMENTO = """CASE
        WHEN b.CANCELED          = 1 THEN 'Cancelado'
        WHEN b.AUTHORIZEXTSTATUS = 1 THEN 'Autorizado (operadora)'
        WHEN b.AUTHORIZINTSTATUS = 1 THEN 'Autorizado (interno)'
        WHEN b.LIBERATED         = 1 THEN 'Liberado'
        ELSE 'Pendente'
    END"""

# Junta a admissão à dimensão canônica de unidade. Três joins que se repetem em
# toda consulta de orçamento.
JOIN_UNIDADE_ORCAMENTO = """JOIN CAPADMISSION a ON a.ID = b.IDADMISSION
LEFT JOIN GLBHEALTHPROVDEP d ON d.ID = a.IDHEALTHPROVDEP
LEFT JOIN GLBHEALTHPROVIDER hp ON hp.ID = d.IDHEALTHPROVIDER"""


# ---------------------------------------------------------------------------
# 1. Unidades de negócio
# ---------------------------------------------------------------------------

SQL_UNIDADES_FATURAM = """
SELECT e.ID        AS id_homecare,
       e.NAME      AS unidade,
       e.FULLNAME  AS razao_social,
       e.IDNUMBER1 AS cnpj
  FROM GLBENTERPRISE e
 WHERE EXISTS (SELECT 1 FROM CAPPAYMENT p WHERE p.IDHOMECARE = e.ID)
 ORDER BY e.NAME
"""

SQL_UNIDADES_CANONICAS = """
SELECT hp.ID             AS id_health_provider,
       hp.NAME           AS unidade_negocio,
       hp.HEALTHCARETYPE AS tipo_assistencia,
       hp.INACTIVE       AS inativa,
       e.ID              AS id_homecare,
       e.NAME            AS empresa_homecare
  FROM GLBHEALTHPROVIDER hp
  LEFT JOIN GLBENTERPRISE e ON e.ID = hp.IDENTERPRISE
 ORDER BY hp.NAME
"""

SQL_UNIDADES_MAPA = """
SELECT p.IDHOMECARE,
       hc.NAME AS empresa_homecare,
       LISTAGG(hp.NAME, ' / ') WITHIN GROUP (ORDER BY hp.NAME) AS health_providers
  FROM (SELECT DISTINCT IDHOMECARE FROM CAPPAYMENT WHERE IDHOMECARE IS NOT NULL) p
  JOIN GLBENTERPRISE hc ON hc.ID = p.IDHOMECARE
  LEFT JOIN GLBHEALTHPROVIDER hp ON hp.IDENTERPRISE = p.IDHOMECARE
 GROUP BY p.IDHOMECARE, hc.NAME
 ORDER BY hc.NAME
"""

ESCOPOS_UNIDADE = {
    "faturam": SQL_UNIDADES_FATURAM,
    "canonica": SQL_UNIDADES_CANONICAS,
    "mapa": SQL_UNIDADES_MAPA,
}


def unidades(escopo="faturam", limite=100, formato="json"):
    escopo = (escopo or "faturam").strip().lower()
    sql = ESCOPOS_UNIDADE.get(escopo)
    if sql is None:
        raise PeriodoInvalido(
            f"escopo '{escopo}' não existe",
            "Use 'faturam' (quem aparece no faturamento), 'canonica' (a dimensão "
            "GLBHEALTHPROVIDER, usada em orçamento) ou 'mapa' (a correspondência "
            "entre as duas).",
        )
    return oracle.executar(sql, limite=limite, formato=formato)


# ---------------------------------------------------------------------------
# 2. Operadoras de saúde
# ---------------------------------------------------------------------------

SQL_OPERADORAS = """
SELECT ID, NAME, FULLNAME, IDNUMBER1 AS cnpj
  FROM GLBENTERPRISE
 WHERE HEALTHPLAN = 1
 ORDER BY NAME
"""

SQL_OPERADORAS_FATURAM = """
SELECT e.ID, e.NAME, e.FULLNAME, e.IDNUMBER1 AS cnpj
  FROM GLBENTERPRISE e
 WHERE e.HEALTHPLAN = 1
   AND EXISTS (SELECT 1 FROM CAPPAYMENT p WHERE p.IDENTERPRISE = e.ID)
 ORDER BY e.NAME
"""


def operadoras(apenas_com_faturamento=True, limite=100, formato="json"):
    sql = SQL_OPERADORAS_FATURAM if apenas_com_faturamento else SQL_OPERADORAS
    return oracle.executar(sql, limite=limite, formato=formato)


# ---------------------------------------------------------------------------
# 3 e 4. Faturas (contas)
# ---------------------------------------------------------------------------

SQL_CONTAS_POR_STATUS = f"""
WITH contas AS (
    SELECT p.ID, p.IDHOMECARE, p.IDENTERPRISE,
           p.SIMULATION, p.LIBERATED, p.EXPORTED, p.WRITEOFF, p.COLLECTION,
           SUM({VALOR_ITEM}) AS valor_faturado
      FROM CAPPAYMENT p
      JOIN CAPPAYMENTITEM i ON i.IDPAYMENT = p.ID
     WHERE p.STARTDATE >= :DT_INI
       AND p.STARTDATE <  :DT_FIM
     GROUP BY p.ID, p.IDHOMECARE, p.IDENTERPRISE,
              p.SIMULATION, p.LIBERATED, p.EXPORTED, p.WRITEOFF, p.COLLECTION
), classificadas AS (
    SELECT NVL(hc.NAME, '(sem unidade)') AS unidade,
           {SITUACAO_CONTA.format(a='c')} AS situacao,
           c.valor_faturado
      FROM contas c
      LEFT JOIN GLBENTERPRISE hc ON hc.ID = c.IDHOMECARE
     WHERE {FILTRO_HOMECARE}
)
SELECT unidade,
       situacao,
       COUNT(*)                      AS contas,
       ROUND(SUM(valor_faturado), 2) AS valor
  FROM classificadas
 GROUP BY unidade, situacao
 ORDER BY unidade, valor DESC
"""

SQL_CONTAS_DETALHE = f"""
SELECT p.ID                                  AS id_conta,
       p.STARTDATE                           AS competencia_inicio,
       p.ENDDATE                             AS competencia_fim,
       NVL(hc.NAME, '(sem unidade)')         AS unidade,
       NVL(op.NAME, '(particular/paciente)') AS operadora,
       {SITUACAO_CONTA.format(a='p')}        AS situacao,
       p.LIBERATED, p.EXPORTED, p.WRITEOFF, p.COLLECTION,
       p.AUTHORIZEXTCODE                     AS autorizacao_externa,
       ROUND(SUM({VALOR_ITEM}), 2)           AS valor_faturado
  FROM CAPPAYMENT p
  JOIN CAPPAYMENTITEM i ON i.IDPAYMENT = p.ID
  LEFT JOIN GLBENTERPRISE hc ON hc.ID = p.IDHOMECARE
  LEFT JOIN GLBENTERPRISE op ON op.ID = p.IDENTERPRISE
 WHERE p.STARTDATE >= :DT_INI
   AND p.STARTDATE <  :DT_FIM
   AND {FILTRO_HOMECARE}
 GROUP BY p.ID, p.STARTDATE, p.ENDDATE, hc.NAME, op.NAME,
          p.SIMULATION, p.LIBERATED, p.EXPORTED, p.WRITEOFF, p.COLLECTION,
          p.AUTHORIZEXTCODE
 ORDER BY valor_faturado DESC
"""


def contas_por_status(inicio, fim, unidade="", limite=100, formato="json"):
    return oracle.executar(
        SQL_CONTAS_POR_STATUS, limite=limite, formato=formato,
        params=_params(inicio, fim, unidade),
    )


def contas_detalhe(inicio, fim, unidade="", limite=100, formato="json"):
    return oracle.executar(
        SQL_CONTAS_DETALHE, limite=limite, formato=formato,
        params=_params(inicio, fim, unidade),
    )


# ---------------------------------------------------------------------------
# 5 e 6. Orçamentos
# ---------------------------------------------------------------------------

SQL_ORCAMENTOS_POR_STATUS = f"""
WITH classificados AS (
    SELECT NVL(hp.NAME, '(sem unidade)') AS unidade,
           NVL(hc.NAME, '')              AS empresa_homecare,
           {SITUACAO_ORCAMENTO}          AS situacao,
           COALESCE(b.BUDGETCHARGE, b.ANALYSISCHARGE, 0) AS valor
      FROM CAPBUDGET b
      {JOIN_UNIDADE_ORCAMENTO}
      LEFT JOIN GLBENTERPRISE hc ON hc.ID = hp.IDENTERPRISE
     WHERE b.STARTDATE >= :DT_INI
       AND b.STARTDATE <  :DT_FIM
       AND {FILTRO_PROVIDER_OU_HOMECARE}
)
SELECT unidade,
       empresa_homecare,
       situacao,
       COUNT(*)             AS orcamentos,
       ROUND(SUM(valor), 2) AS valor_previsto
  FROM classificados
 GROUP BY unidade, empresa_homecare, situacao
 ORDER BY unidade, valor_previsto DESC
"""

SQL_ORCAMENTOS_DETALHE = f"""
SELECT b.ID                          AS id_orcamento,
       b.STARTDATE                   AS inicio,
       b.ENDDATE                     AS fim,
       NVL(hp.NAME, '(sem unidade)') AS unidade,
       {SITUACAO_ORCAMENTO}          AS situacao,
       b.AUTHORIZEXTCODE             AS senha_autorizacao,
       b.BUDGETCHARGE                AS valor_previsto,
       b.COMMENTS
  FROM CAPBUDGET b
  {JOIN_UNIDADE_ORCAMENTO}
 WHERE b.STARTDATE >= :DT_INI
   AND b.STARTDATE <  :DT_FIM
   AND {FILTRO_PROVIDER}
 ORDER BY b.STARTDATE DESC
"""


def orcamentos_por_status(inicio, fim, unidade="", limite=100, formato="json"):
    return oracle.executar(
        SQL_ORCAMENTOS_POR_STATUS, limite=limite, formato=formato,
        params=_params(inicio, fim, unidade),
    )


def orcamentos_detalhe(inicio, fim, unidade="", limite=100, formato="json"):
    return oracle.executar(
        SQL_ORCAMENTOS_DETALHE, limite=limite, formato=formato,
        params=_params(inicio, fim, unidade),
    )


# ---------------------------------------------------------------------------
# 7. Gap orçamento -> faturamento
# ---------------------------------------------------------------------------

SQL_GAP_DETALHE = f"""
SELECT b.ID                          AS id_orcamento,
       b.STARTDATE                   AS inicio,
       b.BUDGETCHARGE                AS valor_autorizado,
       NVL(hp.NAME, '(sem unidade)') AS unidade
  FROM CAPBUDGET b
  {JOIN_UNIDADE_ORCAMENTO}
 WHERE b.AUTHORIZEXTSTATUS = 1
   AND b.CANCELED = 0
   AND b.STARTDATE >= :DT_INI
   AND b.STARTDATE <  :DT_FIM
   AND {FILTRO_PROVIDER}
   AND NOT EXISTS (SELECT 1 FROM CAPPAYMENTITEM pi WHERE pi.IDBUDGET = b.ID)
 ORDER BY b.STARTDATE
"""

SQL_GAP_RESUMO = f"""
SELECT NVL(hp.NAME, '(sem unidade)')              AS unidade,
       COUNT(*)                                   AS autorizados_sem_faturamento,
       ROUND(SUM(COALESCE(b.BUDGETCHARGE, 0)), 2) AS valor_autorizado_pendente
  FROM CAPBUDGET b
  {JOIN_UNIDADE_ORCAMENTO}
 WHERE b.AUTHORIZEXTSTATUS = 1
   AND b.CANCELED = 0
   AND b.STARTDATE >= :DT_INI
   AND b.STARTDATE <  :DT_FIM
   AND {FILTRO_PROVIDER}
   AND NOT EXISTS (SELECT 1 FROM CAPPAYMENTITEM pi WHERE pi.IDBUDGET = b.ID)
 GROUP BY NVL(hp.NAME, '(sem unidade)')
 ORDER BY valor_autorizado_pendente DESC
"""


def gap_orcamento_faturamento(inicio, fim, unidade="", detalhado=False,
                              limite=100, formato="json"):
    sql = SQL_GAP_DETALHE if detalhado else SQL_GAP_RESUMO
    return oracle.executar(
        sql, limite=limite, formato=formato, params=_params(inicio, fim, unidade)
    )


# ---------------------------------------------------------------------------
# 8, 9 e 10. Glosas
# ---------------------------------------------------------------------------

SQL_GLOSA_POR_UNIDADE = f"""
SELECT NVL(hc.NAME, '(sem unidade)')                     AS unidade,
       COUNT(*)                                          AS itens,
       COUNT(CASE WHEN i.AUDITBILLVALUE > 0 THEN 1 END)  AS itens_glosados,
       ROUND(SUM({VALOR_ITEM}), 2)                       AS faturado,
       ROUND(SUM(NVL(i.AUDITBILLVALUE, 0)), 2)           AS glosa,
       ROUND(SUM(NVL(i.AUDITBILLSUSTAINED, 0)), 2)       AS glosa_sustentada,
       ROUND(SUM(NVL(i.AUDITBILLRECOVERED, 0)), 2)       AS glosa_recuperada,
       ROUND(SUM(NVL(i.AUDITBILLVALUE, 0)) /
             NULLIF(SUM({VALOR_ITEM}), 0) * 100, 2)      AS taxa_glosa_pct
  FROM CAPPAYMENTITEM i
  JOIN CAPPAYMENT p ON p.ID = i.IDPAYMENT
  LEFT JOIN GLBENTERPRISE hc ON hc.ID = p.IDHOMECARE
 WHERE p.SIMULATION = 0
   AND p.STARTDATE >= :DT_INI
   AND p.STARTDATE <  :DT_FIM
   AND {FILTRO_HOMECARE}
 GROUP BY NVL(hc.NAME, '(sem unidade)')
 ORDER BY glosa DESC
"""

SQL_GLOSA_POR_OPERADORA = f"""
SELECT NVL(op.NAME, '(particular/paciente)')       AS operadora,
       COUNT(*)                                    AS itens_glosados,
       ROUND(SUM(NVL(i.AUDITBILLVALUE, 0)), 2)     AS glosa,
       ROUND(SUM(NVL(i.AUDITBILLSUSTAINED, 0)), 2) AS glosa_sustentada,
       ROUND(SUM(NVL(i.AUDITBILLRECOVERED, 0)), 2) AS glosa_recuperada
  FROM CAPPAYMENTITEM i
  JOIN CAPPAYMENT p ON p.ID = i.IDPAYMENT
  LEFT JOIN GLBENTERPRISE hc ON hc.ID = p.IDHOMECARE
  LEFT JOIN GLBENTERPRISE op ON op.ID = p.IDENTERPRISE
 WHERE p.SIMULATION = 0
   AND p.STARTDATE >= :DT_INI
   AND p.STARTDATE <  :DT_FIM
   AND {FILTRO_HOMECARE}
   AND i.AUDITBILLVALUE > 0
 GROUP BY NVL(op.NAME, '(particular/paciente)')
 ORDER BY glosa DESC
"""

# qtd_glosada (AUDITBILLQUANTITY) não está no documento original e entra aqui
# porque é o par natural do valor glosado -- e, medido em 2025, vem preenchida em
# 3.140 dos 3.145 itens com glosa. cod_glosa_tiss (AUDITBILLCODE) fica, embora
# esteja vazia em 100% dos casos hoje: o dia em que o TISS passar a ser gravado,
# ela aparece sozinha.
SQL_GLOSA_DETALHE = """
SELECT NVL(hc.NAME, '(sem unidade)')        AS unidade,
       NVL(op.NAME, '(particular)')         AS operadora,
       cat.NAME                             AS categoria_recurso,
       sc.CODENAME                          AS recurso,
       i.QUANTITY                           AS qtd,
       i.AUDITBILLQUANTITY                  AS qtd_glosada,
       ROUND(i.AUDITBILLVALUE, 2)           AS glosa,
       ROUND(NVL(i.AUDITBILLSUSTAINED, 0), 2) AS sustentada,
       ROUND(NVL(i.AUDITBILLRECOVERED, 0), 2) AS recuperada,
       i.AUDITBILLSTATUS                    AS status,
       i.AUDITBILLREASON                    AS cod_recurso,
       i.AUDITBILLCODE                      AS cod_glosa_tiss,
       i.AUDITBILLCOMMENTS                  AS motivo_texto
  FROM CAPPAYMENTITEM i
  JOIN CAPPAYMENT p ON p.ID = i.IDPAYMENT
  LEFT JOIN GLBENTERPRISE hc ON hc.ID = p.IDHOMECARE
  LEFT JOIN GLBENTERPRISE op ON op.ID = p.IDENTERPRISE
  LEFT JOIN CTRRESOURCETYPE rt ON rt.RESOURCETYPE = i.RESOURCETYPE
  LEFT JOIN SCCTABLE cat ON cat.ID = rt.IDSCTYPE
  LEFT JOIN SCCCODE sc ON sc.ID = i.SCRESOURCE
 WHERE p.SIMULATION = 0
   AND p.STARTDATE >= :DT_INI
   AND p.STARTDATE <  :DT_FIM
   AND {filtro}
   AND i.AUDITBILLVALUE > 0
 ORDER BY i.AUDITBILLVALUE DESC
""".format(filtro=FILTRO_HOMECARE)


def glosa_por_unidade(inicio, fim, unidade="", limite=100, formato="json"):
    return oracle.executar(
        SQL_GLOSA_POR_UNIDADE, limite=limite, formato=formato,
        params=_params(inicio, fim, unidade),
    )


def glosa_por_operadora(inicio, fim, unidade="", limite=100, formato="json"):
    return oracle.executar(
        SQL_GLOSA_POR_OPERADORA, limite=limite, formato=formato,
        params=_params(inicio, fim, unidade),
    )


def glosa_detalhe(inicio, fim, unidade="", limite=100, formato="json"):
    return oracle.executar(
        SQL_GLOSA_DETALHE, limite=limite, formato=formato,
        params=_params(inicio, fim, unidade),
    )


# ---------------------------------------------------------------------------
# 11 e 12. Motivos de glosa
# ---------------------------------------------------------------------------

# A classificação é heurística, por texto livre, e as regras são as do documento
# original -- inclusive os curingas que cobrem acento ('%C%DIGO%' pega CODIGO e
# CÓDIGO). Aqui o CASE aparece UMA vez, dentro da CTE; no original ele aparecia
# duas, porque o Oracle não aceita alias no GROUP BY. Mesmo resultado, metade do
# texto, e sem o risco de as duas cópias divergirem numa manutenção.
#
# A ORDEM DOS RAMOS É A REGRA DE NEGÓCIO. O primeiro que casa vence, então onde
# uma regra entra decide de quem ela tira. As três acrescentadas na v2:
#
# 1. Placeholder, no TOPO. A operadora grava '.' (ou ';', ou só espaço) no lugar
#    do motivo, e isso não é motivo -- é ausência dele. Ficava em 'Outros' porque
#    a cláusula antiga só pegava NULL e branco: TRIM('.') é '.', não é nulo.
#    Medido na base inteira: 1.190 itens, R$ 2,18 mi mudam para 'Sem narrativa'.
#    Esta cláusula absorveu a antiga (all-whitespace casa o mesmo regex, e string
#    vazia no Oracle É nulo), por isso a do fim saiu.
#
# 2 e 3. Liminar e Documentação, no FIM, logo antes do ELSE. Aqui a posição foi
#    medida, não escolhida no gosto: 'ANEXO|DOCUMENTA|GUIA|FATURA' aparece no
#    texto de 2.725 itens, R$ 3,45 mi -- mas só 883 deles (R$ 1,38 mi) estão em
#    'Outros'. Os outros já têm motivo próprio, e o maior grupo é 'Divergência de
#    senha/autorização' (817 itens, R$ 1,15 mi). Pôr a regra no topo esvaziaria
#    essa categoria e trocaria um motivo real ("a senha diverge") pela instrução
#    de recurso que vem junto ("segue documentação em anexo"). No fim, ela resgata
#    de 'Outros' sem tocar em nada que já estava classificado.
CLASSIFICACAO_MOTIVO = """CASE
        WHEN i.AUDITBILLCOMMENTS IS NULL
          OR REGEXP_LIKE(i.AUDITBILLCOMMENTS, '^[[:space:].;]+$')
            THEN 'Sem narrativa'
        WHEN UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%DATA DE CADASTRO%'
            THEN 'Data de cadastro indevida'
        WHEN UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%DATA POSTERIOR%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%LIBERA%O DA SENHA%'
            THEN 'Autorização tardia (senha pós-início)'
        WHEN UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%SENHA%'
            THEN 'Divergência de senha/autorização'
        WHEN UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%DIVERG%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%ATUALIZA% SIST%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%C%DIGO%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%TISS%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%TUSS%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%BRASINDICE%'
            THEN 'Divergência de código'
        WHEN UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%UNIDADE DE MEDIDA%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '% LATA%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%GRAMAS%'
            THEN 'Unidade de medida'
        WHEN UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%VALIDADE%'
            THEN 'Período de validade'
        WHEN UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%CONCESS%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%VISITA%'
            THEN 'Visitas/concessão'
        WHEN UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%OR%AMENTO%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%PRORROGA%'
            THEN 'Orçamento/prorrogação'
        WHEN UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%CONTRATO%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%NEGOCIA%'
            THEN 'Contrato/negociação'
        WHEN UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%LIMINAR%'
            THEN 'Liminar judicial'
        WHEN UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%ANEXO%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%DOCUMENTA%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%GUIA%'
          OR UPPER(NVL(i.AUDITBILLCOMMENTS,' ')) LIKE '%FATURA%'
            THEN 'Documentação/autorização em anexo'
        ELSE 'Outros'
    END"""

SQL_MOTIVOS_TAXONOMIA = f"""
WITH classificados AS (
    SELECT {CLASSIFICACAO_MOTIVO} AS motivo,
           i.AUDITBILLVALUE       AS valor
      FROM CAPPAYMENTITEM i
      JOIN CAPPAYMENT p ON p.ID = i.IDPAYMENT
      LEFT JOIN GLBENTERPRISE hc ON hc.ID = p.IDHOMECARE
     WHERE p.SIMULATION = 0
       AND p.STARTDATE >= :DT_INI
       AND p.STARTDATE <  :DT_FIM
       AND {FILTRO_HOMECARE}
       AND i.AUDITBILLVALUE > 0
)
SELECT motivo,
       COUNT(*)             AS itens,
       ROUND(SUM(valor), 2) AS valor_glosa
  FROM classificados
 GROUP BY motivo
 ORDER BY valor_glosa DESC
"""

# Sem o DISTINCT do original: com GROUP BY na mesma coluna ele é no-op.
SQL_MOTIVOS_TEXTO = f"""
SELECT i.AUDITBILLCOMMENTS            AS motivo_texto,
       COUNT(*)                       AS ocorrencias,
       ROUND(SUM(i.AUDITBILLVALUE), 2) AS valor_glosa
  FROM CAPPAYMENTITEM i
  JOIN CAPPAYMENT p ON p.ID = i.IDPAYMENT
  LEFT JOIN GLBENTERPRISE hc ON hc.ID = p.IDHOMECARE
 WHERE p.SIMULATION = 0
   AND p.STARTDATE >= :DT_INI
   AND p.STARTDATE <  :DT_FIM
   AND {FILTRO_HOMECARE}
   AND i.AUDITBILLVALUE > 0
   AND i.AUDITBILLCOMMENTS IS NOT NULL
 GROUP BY i.AUDITBILLCOMMENTS
 ORDER BY valor_glosa DESC
"""


def motivos_taxonomia(inicio, fim, unidade="", limite=100, formato="json"):
    return oracle.executar(
        SQL_MOTIVOS_TAXONOMIA, limite=limite, formato=formato,
        params=_params(inicio, fim, unidade),
    )


def motivos_texto(inicio, fim, unidade="", limite=100, formato="json"):
    return oracle.executar(
        SQL_MOTIVOS_TEXTO, limite=limite, formato=formato,
        params=_params(inicio, fim, unidade),
    )


# ---------------------------------------------------------------------------
# 13 e 14. Faturamento e contas em aberto por operadora
# ---------------------------------------------------------------------------

SQL_FATURAMENTO_POR_OPERADORA = f"""
SELECT NVL(hc.NAME, '(sem unidade)')         AS unidade,
       NVL(op.NAME, '(particular/paciente)') AS operadora,
       COUNT(DISTINCT p.ID)                  AS contas,
       ROUND(SUM({VALOR_ITEM}), 2)           AS valor_faturado
  FROM CAPPAYMENT p
  JOIN CAPPAYMENTITEM i ON i.IDPAYMENT = p.ID
  LEFT JOIN GLBENTERPRISE hc ON hc.ID = p.IDHOMECARE
  LEFT JOIN GLBENTERPRISE op ON op.ID = p.IDENTERPRISE
 WHERE p.SIMULATION = 0
   AND p.STARTDATE >= :DT_INI
   AND p.STARTDATE <  :DT_FIM
   AND {FILTRO_HOMECARE}
 GROUP BY NVL(hc.NAME, '(sem unidade)'), NVL(op.NAME, '(particular/paciente)')
 ORDER BY valor_faturado DESC
"""

SQL_CONTAS_EM_ABERTO = f"""
SELECT NVL(op.NAME, '(particular/paciente)') AS operadora,
       COUNT(*)                              AS contas_abertas,
       ROUND(SUM({VALOR_ITEM}), 2)           AS valor_aberto
  FROM CAPPAYMENT p
  JOIN CAPPAYMENTITEM i ON i.IDPAYMENT = p.ID
  LEFT JOIN GLBENTERPRISE hc ON hc.ID = p.IDHOMECARE
  LEFT JOIN GLBENTERPRISE op ON op.ID = p.IDENTERPRISE
 WHERE p.SIMULATION = 0
   AND p.WRITEOFF = 0
   AND (p.LIBERATED = 0 OR p.EXPORTED = 0)
   AND p.STARTDATE >= :DT_INI
   AND p.STARTDATE <  :DT_FIM
   AND {FILTRO_HOMECARE}
 GROUP BY NVL(op.NAME, '(particular/paciente)')
 ORDER BY valor_aberto DESC
"""


def faturamento_por_operadora(inicio, fim, unidade="", limite=100, formato="json"):
    return oracle.executar(
        SQL_FATURAMENTO_POR_OPERADORA, limite=limite, formato=formato,
        params=_params(inicio, fim, unidade),
    )


def contas_em_aberto(inicio, fim, unidade="", limite=100, formato="json"):
    return oracle.executar(
        SQL_CONTAS_EM_ABERTO, limite=limite, formato=formato,
        params=_params(inicio, fim, unidade),
    )


# ---------------------------------------------------------------------------
# 15. Auditoria de contas
# ---------------------------------------------------------------------------

# TTMPAUDITBILLCTR é staging e ACUMULA execuções: 1,7 milhão de linhas para 13
# unidades, em 5.575 execuções. Somar sem prender no TRANSKEY mais recente
# multiplica tudo por um fator que varia por unidade -- daí a CTE, que não é
# otimização e sim a diferença entre o número certo e um número inventado.
SQL_AUDITORIA_ULTIMA = f"""
WITH ultima_execucao AS (
    SELECT IDHOMECARE, MAX(TRANSKEY) AS mxt
      FROM TTMPAUDITBILLCTR
     GROUP BY IDHOMECARE
)
SELECT NVL(hc.NAME, '(sem unidade)')                      AS unidade,
       t.INVOICESTATUS                                    AS invoice_status,
       COUNT(*)                                           AS docs,
       ROUND(SUM(t.DOCWVALUE), 2)                         AS declarado,
       ROUND(SUM(NVL(t.RECOGNIZEDVALUE, 0)), 2)           AS reconhecido,
       ROUND(SUM(t.DOCWVALUE - NVL(t.RECOGNIZEDVALUE, 0)), 2) AS nao_reconhecido
  FROM TTMPAUDITBILLCTR t
  JOIN ultima_execucao u ON u.IDHOMECARE = t.IDHOMECARE AND u.mxt = t.TRANSKEY
  LEFT JOIN GLBENTERPRISE hc ON hc.ID = t.IDHOMECARE
 WHERE {FILTRO_HOMECARE}
 GROUP BY NVL(hc.NAME, '(sem unidade)'), t.INVOICESTATUS
 ORDER BY unidade, t.INVOICESTATUS
"""


def auditoria_ultima_execucao(unidade="", limite=100, formato="json"):
    """Sem período: a tabela de staging não tem data de competência."""
    return oracle.executar(
        SQL_AUDITORIA_ULTIMA, limite=limite, formato=formato,
        params={"UNIDADE": (unidade or "").strip() or None},
    )
