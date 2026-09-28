"""Execução de SQL de LEITURA contra o Oracle de origem.

Este módulo é o único ponto do servidor que fala com o banco de negócio. Os
demais (busca, listagem, objeto) falam com o CATÁLOGO, no Postgres -- lá está o
metadado; aqui está o dado. A ordem de uso é sempre essa: descobrir a tabela no
catálogo, depois consultar aqui.

A conexão é a mesma do projeto ~/apps/opalus-data-connect (scripts/mapear_oracle.py):
mesmo host, mesmo service, mesmo usuário de leitura, driver `oracledb` em modo
thin. O banco fica em rede interna, então sem VPN o connect estoura em timeout,
não em erro de credencial -- o `erro` devolvido distingue os dois casos.

Três garantias que este módulo dá ao agente do outro lado:

1. Só SELECT. A validação recusa DML, DDL e bloco PL/SQL antes de mandar para o
   banco, e a sessão ainda abre como READ ONLY -- se algo escapar da validação,
   o Oracle recusa a escrita.
2. Sempre com teto. A consulta do usuário vira subquery de um ROWNUM, então o
   limite vale mesmo quando ele não escreveu nenhum, e vale mesmo quando ele
   escreveu um FETCH FIRST maior. Não há caminho por onde uma tabela de 341
   milhões de linhas volte inteira para dentro do contexto.
3. Volume previsível. Texto longo sai truncado em LIMITE_TEXTO e BLOB não
   trafega: vira um resumo com tamanho e os primeiros bytes em hex.

O retorno é JSON (list[dict]) ou tabela em Markdown, conforme `formato`. O
Markdown existe porque o cliente é um chat: dez linhas por vinte colunas em JSON
custam o dobro dos tokens e ninguém lê.
"""

import datetime
import decimal
import logging
import os
import re
import time

import sqlparse

logger = logging.getLogger(__name__)

# Teto de linhas. O padrão é o que cabe numa resposta de chat sem estourar o
# contexto; o máximo é o que ainda dá para trafegar sem travar o cliente.
LIMITE_PADRAO = 100
LIMITE_MAXIMO = 1000

# Texto acima disto sai truncado. Mesmo número do catálogo (mapear_oracle.py):
# amostra de dado serve para entender o formato, não para carregar um laudo
# inteiro para dentro do prompt.
LIMITE_TEXTO = 500

# Quantos bytes do início do BLOB mostrar em hex. 16 bastam para reconhecer o
# formato: 25504446 = PDF, FFD8FF = JPEG, 89504E47 = PNG.
BYTES_DO_BLOB = 16

# Comandos que não têm o que fazer num servidor de consulta. MERGE e as DDL
# entram porque escrevem; COMMIT/ROLLBACK/SAVEPOINT porque só fazem sentido
# junto de escrita; BEGIN/DECLARE/CALL/EXECUTE porque abrem PL/SQL, que escapa
# de qualquer análise sintática que se faça aqui; INTO porque `SELECT ... INTO`
# só existe dentro de PL/SQL.
PROIBIDOS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT",
    "CREATE", "DROP", "ALTER", "TRUNCATE", "RENAME", "COMMENT",
    "GRANT", "REVOKE", "AUDIT", "FLASHBACK", "PURGE",
    "COMMIT", "ROLLBACK", "SAVEPOINT", "LOCK",
    "BEGIN", "DECLARE", "CALL", "EXECUTE", "EXEC", "INTO",
}

# CURRENT_SCHEMA vem do .env e entra na sentença por interpolação -- bind não
# vale em ALTER SESSION. Daí o filtro: nome de schema é identificador, não texto.
NOME_DE_SCHEMA = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")


class ConsultaBloqueada(Exception):
    """A SQL não passou na validação. Não chegou a ir ao banco."""

    def __init__(self, motivo, detalhe=""):
        super().__init__(motivo)
        self.motivo = motivo
        self.detalhe = detalhe


class OracleIndisponivel(Exception):
    """Não deu para conectar: VPN fora, host errado, credencial recusada."""

    def __init__(self, motivo, detalhe=""):
        super().__init__(motivo)
        self.motivo = motivo
        self.detalhe = detalhe


class ExecucaoFalhou(Exception):
    """Conectou, mas o Oracle recusou a SQL (ORA-xxxxx)."""

    def __init__(self, motivo, detalhe=""):
        super().__init__(motivo)
        self.motivo = motivo
        self.detalhe = detalhe


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------


def _sem_aspas_simples(valor):
    """Tira UM par de aspas SIMPLES em volta do valor. Só ele, e só simples.

    Existe porque os dois leitores de .env do projeto discordam: o python-dotenv
    (execução na máquina) tira o par externo, e o env_file do docker compose
    (execução no container) NÃO tira -- medido, o valor chega literal, com as
    aspas. O usuário deste banco é `rodrigo.julian`: o ponto exige aspas duplas
    até o Oracle, e sem elas o nome ainda viraria RODRIGO.JULIAN em maiúsculas.
    Então o .env grava ORACLE_USER='"rodrigo.julian"', com simples por fora e
    duplas por dentro, e aqui se tira o par simples que tiver sobrado -- o que
    já passou pelo dotenv não tem nenhum e sai intacto.

    Daí a regra do .env: para proteger um valor (senha com # ou espaço, nome
    case-sensitive), use aspas SIMPLES por fora. As duplas são do Oracle.
    """
    if len(valor) >= 2 and valor[0] == valor[-1] == "'":
        return valor[1:-1]
    return valor


def _config():
    """Lê a conexão do ambiente. Mesmas variáveis do opalus-data-connect."""
    faltando = [
        chave
        for chave in ("ORACLE_HOST", "ORACLE_SERVICE", "ORACLE_USER", "ORACLE_PASSWORD")
        if not os.getenv(chave)
    ]
    if faltando:
        raise OracleIndisponivel(
            "conexão com o Oracle não configurada",
            "faltam no ambiente: " + ", ".join(faltando),
        )
    return {
        "host": os.getenv("ORACLE_HOST"),
        "porta": os.getenv("ORACLE_PORT", "1521"),
        "service": os.getenv("ORACLE_SERVICE"),
        "usuario": _sem_aspas_simples(os.getenv("ORACLE_USER")),
        "senha": _sem_aspas_simples(os.getenv("ORACLE_PASSWORD")),
        "schema": os.getenv("ORACLE_SCHEMA", ""),
        "lib_dir": os.getenv("ORACLE_CLIENT_LIB_DIR", ""),
        "timeout": float(os.getenv("ORACLE_TIMEOUT", "60")),
    }


def _conectar(cfg):
    import oracledb

    # CLOB chega como str em vez de handle; sem isso o valor morreria junto com
    # o cursor e chegaria aqui como objeto inútil.
    oracledb.defaults.fetch_lobs = False
    if cfg["lib_dir"]:
        oracledb.init_oracle_client(lib_dir=cfg["lib_dir"])

    dsn = f'{cfg["host"]}:{cfg["porta"]}/{cfg["service"]}'
    try:
        conexao = oracledb.connect(user=cfg["usuario"], password=cfg["senha"], dsn=dsn)
    except oracledb.Error as erro:
        texto = str(erro)
        if "DPY-3010" in texto:
            dica = (
                "o servidor é anterior ao Oracle 12.1 e o modo thin não atende: "
                "aponte ORACLE_CLIENT_LIB_DIR para o Instant Client"
            )
        elif "DPY-6005" in texto or "DPY-4011" in texto or "timeout" in texto.lower():
            dica = f"não há rota até {dsn} -- a VPN provavelmente está fora"
        elif "ORA-01017" in texto:
            dica = "usuário ou senha recusados (confira as aspas em ORACLE_USER no .env)"
        else:
            dica = texto
        raise OracleIndisponivel(f"não foi possível conectar em {dsn}", dica) from erro

    conexao.call_timeout = int(cfg["timeout"] * 1000)

    cursor = conexao.cursor()
    # CURRENT_SCHEMA para o schema do negócio: sem isso `SELECT * FROM CAPADMISSION`
    # procuraria na conta do usuário de leitura, que não tem tabela nenhuma, e o
    # agente teria de qualificar tudo à mão.
    if cfg["schema"]:
        if not NOME_DE_SCHEMA.match(cfg["schema"]):
            raise OracleIndisponivel(
                "ORACLE_SCHEMA inválido", f'{cfg["schema"]!r} não é um nome de schema'
            )
        cursor.execute(f'ALTER SESSION SET CURRENT_SCHEMA = {cfg["schema"]}')
    # Segunda trava, agora no servidor: a partir daqui a sessão não escreve nem
    # que a validação daqui falhe. Precisa vir antes de qualquer SELECT, que é
    # o que abriria a transação.
    cursor.execute("SET TRANSACTION READ ONLY")
    cursor.close()
    return conexao


# ---------------------------------------------------------------------------
# Validação e limite
# ---------------------------------------------------------------------------


def validar(sql):
    """Devolve a SQL limpa, ou levanta ConsultaBloqueada dizendo por quê."""
    if not sql or not sql.strip():
        raise ConsultaBloqueada("SQL vazia")

    limpa = sql.strip().rstrip(";").strip()

    sentencas = [s for s in sqlparse.parse(limpa) if str(s).strip()]
    if not sentencas:
        raise ConsultaBloqueada("SQL vazia")
    if len(sentencas) > 1:
        raise ConsultaBloqueada(
            "mande uma sentença por vez",
            f"vieram {len(sentencas)}; o driver não executa lote e o teto de "
            "linhas só se aplica a uma consulta",
        )

    primeira = sentencas[0].token_first(skip_cm=True)
    inicio = primeira.value.upper() if primeira else ""
    if inicio not in ("SELECT", "WITH"):
        raise ConsultaBloqueada(
            "só consulta de leitura",
            f"a sentença começa com {inicio or '?'}; use SELECT ou WITH",
        )

    for token in sentencas[0].flatten():
        if token.is_keyword and token.normalized.upper() in PROIBIDOS:
            raise ConsultaBloqueada(
                f"comando {token.normalized.upper()} não é permitido",
                "este servidor é somente leitura",
            )

    return limpa


def com_limite(sql, limite):
    """Põe a consulta dentro de um ROWNUM, com uma linha a mais para detectar corte.

    O teto vai por fora, e não appendado na consulta do usuário, por dois
    motivos: vale mesmo quando ele escreveu o próprio FETCH FIRST maior, e não
    depende de adivinhar onde encaixar a cláusula numa SQL com ORDER BY, UNION
    ou WITH. Em Oracle o padrão é este mesmo -- o ORDER BY da subquery é
    aplicado antes do ROWNUM, então top-N ordenado continua correto.
    """
    return f"SELECT * FROM (\n{sql}\n) WHERE ROWNUM <= :limite_opalus", limite + 1


# ---------------------------------------------------------------------------
# Conversão de valor
# ---------------------------------------------------------------------------


def converter(valor):
    """O que o Oracle devolve vira algo serializável em JSON."""
    if valor is None or isinstance(valor, bool):
        return valor
    if isinstance(valor, str):
        if len(valor) > LIMITE_TEXTO:
            return valor[:LIMITE_TEXTO] + f"... <truncado, {len(valor)} caracteres>"
        return valor
    if isinstance(valor, int):
        return valor
    if isinstance(valor, float):
        # JSON não tem NaN nem Infinity
        return valor if valor == valor and abs(valor) != float("inf") else str(valor)
    if isinstance(valor, decimal.Decimal):
        if not valor.is_finite():
            return str(valor)
        return int(valor) if valor == valor.to_integral_value() else float(valor)
    if isinstance(valor, (datetime.datetime, datetime.date)):
        return valor.isoformat()
    if isinstance(valor, datetime.timedelta):
        return str(valor)
    if isinstance(valor, (bytes, bytearray)):
        inicio = bytes(valor[:BYTES_DO_BLOB]).hex().upper()
        return f"<BINARIO {len(valor)} bytes, inicia em {inicio}>"
    return str(valor)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _celula(valor):
    if valor is None:
        return ""
    texto = str(valor)
    # Pipe fecharia a coluna e quebra de linha fecharia a linha da tabela.
    return texto.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def para_markdown(colunas, linhas):
    """Tabela em Markdown (GFM). Sem linhas, devolve um aviso em vez de casca vazia."""
    if not colunas:
        return "_a consulta não devolveu colunas_"
    cabecalho = "| " + " | ".join(_celula(c) for c in colunas) + " |"
    separador = "| " + " | ".join("---" for _ in colunas) + " |"
    if not linhas:
        return cabecalho + "\n" + separador + "\n\n_nenhuma linha_"
    corpo = [
        "| " + " | ".join(_celula(linha.get(c)) for c in colunas) + " |"
        for linha in linhas
    ]
    return "\n".join([cabecalho, separador, *corpo])


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------


def executar(sql, limite=LIMITE_PADRAO, formato="json", params=None):
    """Valida, limita, executa e devolve o resultado no formato pedido.

    `params` são os binds nomeados da SQL ({"DT_INI": date(...)}), e existem para
    src/faturamento.py: as consultas de lá são fixas e o que varia é o período e a
    unidade. A tool de SQL livre não usa -- lá a consulta inteira vem do agente.

    Bind é o que mantém `run_oracle_query` e as tools de faturamento na mesma
    porta: o valor viaja como valor, nunca concatenado no texto da SQL.

    Atenção ao montar o dicionário: o oracledb recusa bind que a SQL não usa
    (DPY-4008), então cada consulta recebe exatamente as chaves que cita.

    Levanta ConsultaBloqueada, OracleIndisponivel ou ExecucaoFalhou -- os três
    viram payload de erro no main.py, nunca stacktrace.
    """
    formato = (formato or "json").strip().lower()
    if formato in ("md", "markdown", "tabela"):
        formato = "markdown"
    elif formato != "json":
        raise ConsultaBloqueada(
            f"formato '{formato}' não existe", "use 'json' ou 'markdown'"
        )

    try:
        limite = int(limite)
    except (TypeError, ValueError):
        limite = LIMITE_PADRAO
    limite = max(1, min(limite, LIMITE_MAXIMO))

    limpa = validar(sql)
    sql_final, teto = com_limite(limpa, limite)

    cfg = _config()
    comeco = time.monotonic()
    conexao = _conectar(cfg)
    try:
        import oracledb

        with conexao.cursor() as cursor:
            cursor.arraysize = min(teto, 500)
            try:
                cursor.execute(sql_final, {**(params or {}), "limite_opalus": teto})
                colunas = [d[0] for d in cursor.description]
                brutas = cursor.fetchall()
            except oracledb.Error as erro:
                texto = str(erro).strip().splitlines()[0]
                if "ORA-00918" in texto:
                    detalhe = (
                        "duas colunas do resultado têm o mesmo nome. O teto de "
                        "linhas envolve a consulta numa subquery, e aí nome "
                        "repetido não passa -- dê um alias a cada coluna."
                    )
                elif "ORA-00942" in texto:
                    detalhe = (
                        f'tabela ou view não existe em {cfg["schema"] or "seu schema"}. '
                        "Confirme o nome com describe_catalog_table."
                    )
                elif "DPY-4008" in texto or "DPY-4010" in texto:
                    detalhe = (
                        f"{texto} -- a consulta e os parâmetros não batem. O "
                        "oracledb exige que o dicionário tenha exatamente os "
                        "binds citados na SQL, nem um a mais."
                    )
                elif "DPY-4011" in texto or "call timeout" in texto.lower():
                    detalhe = (
                        f'a consulta passou de {cfg["timeout"]:.0f}s e foi cortada. '
                        "Filtre mais, ou reduza o intervalo de datas."
                    )
                else:
                    detalhe = texto
                raise ExecucaoFalhou("o Oracle recusou a consulta", detalhe) from erro
    finally:
        conexao.close()

    truncado = len(brutas) > limite
    linhas = [
        {coluna: converter(valor) for coluna, valor in zip(colunas, bruta)}
        for bruta in brutas[:limite]
    ]
    decorrido = round(time.monotonic() - comeco, 3)
    logger.info(
        "consulta Oracle devolveu %s linhas em %ss (truncado=%s)",
        len(linhas), decorrido, truncado,
    )

    meta = {
        "limite_aplicado": limite,
        "truncado": truncado,
        "schema": cfg["schema"],
        "segundos": decorrido,
        "sql_executada": limpa,
    }
    if truncado:
        meta["aviso"] = (
            f"a consulta tem mais de {limite} linhas e o resultado foi cortado no "
            "teto -- não conclua totais a partir daqui; para contar, use "
            "COUNT(*) na própria SQL"
        )

    if formato == "markdown":
        return {
            "formato": "markdown",
            "markdown": para_markdown(colunas, linhas),
            "colunas": colunas,
            "count": len(linhas),
            **meta,
        }
    return {
        "formato": "json",
        "colunas": colunas,
        "rows": linhas,
        "count": len(linhas),
        **meta,
    }


def status():
    """Testa a conexão e devolve com quem se está falando."""
    cfg = _config()
    comeco = time.monotonic()
    conexao = _conectar(cfg)
    try:
        with conexao.cursor() as cursor:
            cursor.execute(
                "SELECT sys_context('USERENV','SESSION_USER'), "
                "       sys_context('USERENV','CURRENT_SCHEMA'), "
                "       sys_context('USERENV','DB_NAME'), "
                "       systimestamp "
                "  FROM dual"
            )
            usuario, schema, banco, agora = cursor.fetchone()
        versao = conexao.version
    finally:
        conexao.close()
    return {
        "ok": True,
        "host": f'{cfg["host"]}:{cfg["porta"]}/{cfg["service"]}',
        "usuario": usuario,
        "schema": schema,
        "banco": banco,
        "versao": versao,
        "hora_do_banco": converter(agora),
        "segundos": round(time.monotonic() - comeco, 3),
        "limite_padrao": LIMITE_PADRAO,
        "limite_maximo": LIMITE_MAXIMO,
    }
