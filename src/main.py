
import logging
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

load_dotenv()

from src import (busca, catalogo, data_connect, listagem, objeto, oracle,
                 provedores, tools_faturamento, views)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# Hosts liberados no validador de DNS-rebinding. O `mcp` não suporta wildcard de
# subdomínio, então cada subdomínio vai listado explicitamente.
# Extras podem ser adicionados via env ALLOWED_HOSTS (CSV), sem alterar o código.
_ALLOWED_HOSTS = [
    "mcp-server:8001",
    "mcp-server:8002",
    # O serviço streamable-http do compose. O OpenWebUI, ligado como servidor MCP
    # nativo, chama por este nome -- sem ele o validador devolve 421.
    "mcp-http:8002",
    "localhost:*",
    "127.0.0.1:*",
]
_ALLOWED_HOSTS += [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]

mcp = FastMCP(
    "opalus-catalogo",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_ALLOWED_HOSTS,
    ),
    instructions="""
# Opalus Catálogo ORACLE MCP Server

Servidor de consulta ao catálogo de dados: as tabelas e campos do schema Oracle
mapeados para o Postgres, cada um com descrição gerada por IA (rótulo, resumo,
função, domínio) e o vetor dessa descrição.

Use este servidor para descobrir EM QUE TABELA está um dado, antes de escrever
qualquer SQL contra o banco de origem.

## Estratégia geral de uso

1. Para localizar tabelas por assunto, use `search_tables`. É o ponto de entrada
   para quase toda pergunta.
2. Para achar um dado pontual ("onde fica o CPF", "que campo tem a data de
   alta"), use `search_fields`.
3. Quando souber parte do nome ou do rótulo, ou a pergunta for "quais" e
   "quantas", use `list_catalog_tables` -- ela devolve todas as que casam, não
   as mais parecidas, e não custa embedding.
4. Quando o alvo for o campo e não a tabela, use `list_catalog_fields` (LIKE em
   nome e descrição) e `field_relationships` (com o que aquela coluna se liga).
5. Achada a tabela, use `describe_catalog_table` para ver colunas, chaves e
   relacionamentos antes de escrever SQL contra a origem.
6. Para ver que valores um campo assume na prática, use `sample_table_data`. A
   amostra é um retrato datado de até 20 linhas: serve para entender formato,
   nunca para contar, somar ou medir frequência. A máscara dela é parcial, então
   trate o que vier como dado pessoal real.
7. Para o DADO ATUAL -- contar, somar, filtrar, agrupar -- use `run_oracle_query`,
   que executa SELECT no Oracle de origem. Ela é o único caminho até o dado vivo;
   tudo antes dela é metadado. Sempre com teto de linhas, sempre somente leitura.
8. Quando a descrição de uma tabela ou de um campo estiver errada, vaga ou
   desatualizada, corrija-a com `update_table_description` e
   `update_field_descriptions`. Elas mandam o texto novo para o opalus-data-connect,
   que é quem grava o catálogo. Corrigir é barato e melhora a busca de todo mundo
   daí em diante -- mas escreva o que você VERIFICOU nos dados, não o que supôs.
9. Se a busca vier vazia, reformule o termo com o vocabulário do negócio antes
   de concluir que o dado não existe. Nomes de tabela do Oracle são abreviados
   e crípticos; a descrição é que está em português.
10. Use `vector_coverage` para saber quanto do catálogo está vetorizado quando a
   busca parecer cega.

## Relatórios de faturamento, glosa e auditoria

Além das tools de catálogo, o servidor traz 15 consultas de negócio prontas e
conferidas (`billing_*`, `budgets_*`, `denials_*`, `budget_billing_gap`,
`bill_audit_last_run`, `list_business_units`, `list_health_plans`).

PREFIRA ESSAS TOOLS A ESCREVER SQL. Elas já embutem três regras que não se
adivinham lendo o schema, e errar qualquer uma devolve um número plausível e
menor que o real:

- o valor faturado do item depende do tipo: imposto (RESOURCETYPE 15) entra pelo
  custo, o resto pelo preço;
- "unidade de negócio" são DUAS dimensões diferentes, com nomes diferentes;
- o fim do período é exclusivo.

### Período

Todas as tools com recorte de tempo aceitam `mes="AAAA-MM"` -- prefira essa
forma, que já resolve o mês inteiro. O par `dt_ini`/`dt_fim` existe para recorte
livre, e nele o FIM É EXCLUSIVO: julho inteiro é dt_ini="2025-07-01" e
dt_fim="2025-08-01". Não há período padrão; sem ele a tool devolve erro, porque
inventar "o mês passado" produziria um número com cara de resposta.

### Unidade de negócio: duas dimensões, nomes diferentes

- Faturamento, contas, glosa e auditoria usam a EMPRESA (CAPPAYMENT.IDHOMECARE):
  'Premier Brooklin', 'PLENO SAUDE - RJ', 'Geriatrics - Part'...
- Orçamento e admissão usam a dimensão CANÔNICA (GLBHEALTHPROVIDER):
  'Premier Brooklin HSP', 'Premier Brooklin AMB', 'PLENO RJ HC'...

Passar o nome de uma no filtro da outra devolve ZERO linhas, não erro. Na dúvida,
rode `list_business_units` antes -- escopo "faturam", "canonica" ou "mapa" (a
correspondência entre as duas). O filtro ignora caixa e espaço sobrando.

### Glosa chega depois

Glosa depende da auditoria da operadora e aparece meses após a competência.
Medido: junho/2026 tem 1 item glosado em 102 mil; julho/2025 tem 302. Competência
recente com glosa quase zero NÃO é boa notícia -- é auditoria que ainda não
aconteceu. Diga isso ao usuário em vez de apresentar o número como resultado.

## Regras globais

- `relevancia` vai de 0 a 100 e já está calibrada contra o ruído do modelo: 0 é
  o acaso, não "sem similaridade". Trate abaixo de ~25 como pista fraca.
- Não invente tabela, campo ou descrição que não veio no resultado.
- Cite o nome da tabela e do campo em que baseou a resposta.
- Se nada relevante voltar, diga isso e sugira outro termo -- não escolha a
  tabela menos ruim da lista.
- SQL é somente leitura.
""",
)


def _erro(falha):
    """Falha esperada vira payload, não stacktrace.

    Toda exceção destes módulos carrega motivo curto + detalhe acionável; jogar
    a exceção para o FastMCP transformaria isso num traceback que o agente não
    consegue agir em cima.
    """
    logging.warning("%s: %s", type(falha).__name__, falha)
    return {
        "erro": getattr(falha, "motivo", str(falha)),
        "detalhe": getattr(falha, "detalhe", ""),
    }


@mcp.tool(description="""Verifica se o servidor está ativo.""")
def ping():
    logging.info("ping chamada")
    return {"ok": True}


@mcp.tool(description="""
Busca semântica no catálogo de dados do banco oracle: encontra TABELAS assunto.

É o ponto de entrada do servidor. Use sempre que a pergunta for "onde estão os
dados de X", "que tabela guarda Y", "existe campo de Z" -- inclusive antes de
escrever SQL, para descobrir contra o que consultar.

Como funciona: o termo é vetorizado e comparado com o vetor da descrição de cada
tabela e de cada campo, somado a um reforço lexical sobre o nome. Por isso
funciona tanto com linguagem natural ("pacientes internados na UTI") quanto com
o nome técnico ("TDPACIENTE").

Parâmetros:
- termo: o que procurar, em português corrente ou como nome de tabela.
- topk: quantos resultados no máximo. 10 a 20 costuma bastar.
- min_relevancia: piso de relevância (0..100). O padrão já corta o ruído; suba
  para 25 ou 30 se quiser só os resultados fortes, desça para 0 para ver tudo.

Retorna:
- rows: cada linha traz tabela, rotulo, dominio,resumo, registros (nº de linhas na origem) e relevancia; em "ambos" traz ainda
  `evidencia`, os campos que puxaram aquela tabela para cima. 
- count: quantas linhas voltaram.


Interpretação de `relevancia`: a escala já desconta o ruído do modelo, então 0
significa "indistinguível do acaso". Acima de ~40 é forte, 25 a 40 é plausível,
abaixo disso é pista fraca -- confirme com `describe_catalog_table` antes de afirmar.

Se `rows` vier vazio, o dado pode existir sob outro vocabulário. Reformule uma
vez com sinônimos do negócio antes de responder que não existe.

""")
def search_tables(
    termo: str,
    escopo: str = "tabelas",
    topk: int = 20,
    # excluir: str = "",
    min_relevancia: float = busca.MINIMO_RELEVANCIA,
):
    try:
        linhas, meta = busca.buscar(
            termo,
            escopo=escopo,
            topk=topk,
            # excluir=excluir,
            min_relevancia=min_relevancia,
        )
    except (provedores.ConfiguracaoInvalida, busca.embeddings.EmbutirFalhou,
            busca.SemCobertura, catalogo.ConsultaFalhou) as falha:
        return _erro(falha)
    return {"rows": linhas, "count": len(linhas), "meta": meta}

@mcp.tool(description="""
Busca semântica no catálogo de dados do banco oracle: CAMPOS por assunto.

Como funciona: o termo é vetorizado e comparado com o vetor da descrição de cada
de cada campo, somado a um reforço lexical sobre o nome.

Parâmetros:
- termo: o que procurar, em português corrente ou como nome de tabela/campo.
- topk: quantos resultados no máximo. 10 a 20 costuma bastar.

- min_relevancia: piso de relevância (0..100). O padrão já corta o ruído; suba
  para 25 ou 30 se quiser só os resultados fortes, desça para 0 para ver tudo.

Retorna:
- rows: cada linha traz tabela, campo, descricao, registros (nº de linhas da
  TABELA na origem, para situar o campo -- coluna perfeita numa tabela vazia
  não serve para consultar nada) e relevancia.
- count: quantas linhas voltaram.
- meta: modelo usado, escopo, quantas linhas o piso descartou e o tempo da consulta.

Interpretação de `relevancia`: a escala já desconta o ruído do modelo, então 0
significa "indistinguível do acaso". Acima de ~40 é forte, 25 a 40 é plausível,
abaixo disso é pista fraca -- confirme com `describe_catalog_table` antes de afirmar.

Se `rows` vier vazio, o dado pode existir sob outro vocabulário. Reformule uma
vez com sinônimos do negócio antes de responder que não existe.

Cada busca com termo novo custa uma chamada paga de embedding; termos repetidos
saem do cache. Não vale varrer o catálogo com dezenas de variações.
""")
def search_fields(
    termo: str,
    escopo: str = "tabelas",
    topk: int = 20,
    # excluir: str = "",
    min_relevancia: float = busca.MINIMO_RELEVANCIA,
):
    try:
        linhas, meta = busca.buscar(
            termo,
            escopo=escopo,
            topk=topk,
            # excluir=excluir,
            min_relevancia=min_relevancia,
        )
    except (provedores.ConfiguracaoInvalida, busca.embeddings.EmbutirFalhou,
            busca.SemCobertura, catalogo.ConsultaFalhou) as falha:
        return _erro(falha)
    return {"rows": linhas, "count": len(linhas), "meta": meta}


@mcp.tool(description="""
Lista tabelas cujo NOME ou RÓTULO contenha um texto, com o porte e o mapa de FKs.

Complementa as buscas semânticas, e a diferença é o que você já sabe:
- não sabe o nome, sabe o assunto -> `search_tables` / `search_fields` (vetor);
- sabe parte do nome ou do rótulo -> esta tool (LIKE, não custa embedding);
- sabe a tabela e quer tudo dela -> `describe_catalog_table`.

Use quando a pergunta for "quais tabelas de prescrição existem", "liste as
tabelas do módulo CTR", "que tabelas ninguém referencia". A busca vetorial
devolve as 20 mais parecidas com um conceito; esta devolve TODAS as que casam o
texto, na ordem do nome -- é a resposta certa para "quais" e "quantas".

Parâmetros:
- termo: o texto procurado. Varre o nome da tabela E o rótulo da IA no mesmo
  parâmetro, porque o catálogo é bilíngue por acidente: a tabela se chama
  CAPADMISSION e o rótulo diz "Admissões de pacientes". Acento é ignorado dos
  dois lados, então "admissoes" acha "Admissões". Vazio lista tudo.
- tem_dependencias: true traz só as que apontam para outra tabela, false só as
  que não apontam para nenhuma. Omita para não filtrar.
- tem_dependentes: true traz só as que alguma tabela referencia, false só as
  que ninguém referencia. Omita para não filtrar.
  As duas em false acham tabela solta -- sem FK em nenhuma direção, tipicamente
  temporária, de carga ou abandonada.
- max_n_registros: traz só tabelas com no máximo esse nº de linhas na origem.
  0 acha as vazias (578 no catálogo), 100 acha as pequenas (1.287). Combinado
  com os filtros de FK, é como se acha candidata a tabela morta. Omita para não
  filtrar.
  Duas ressalvas: a contagem vem das estatísticas do Oracle e é aproximada em
  TODAS as tabelas (`contagem_aproximada` é sempre true), então não use este
  filtro como corte exato; e as 40 tabelas sem contagem registrada ficam de fora
  do resultado, porque não dá para afirmar que caibam no teto.
- dependencias: inclui a lista do que cada tabela referencia.
- dependentes: inclui a lista de quem referencia cada tabela.
- campos: inclui as colunas (no máximo 30 por tabela, com `campos_omitidos`
  dizendo quantas ficaram de fora).
- limite: máximo de tabelas (padrão 25, teto 100).

Retorna, por linha e sempre:
- tabela, rotulo, dominio, registros (nº de linhas na origem), tem_registros,
  n_colunas, fks_saindo e fks_entrando.

E, quando os booleanos pedirem, `dependencias`, `dependentes` e `campos`.

Atenção à contagem: `fks_saindo` conta CONSTRAINTS, e a lista `dependencias`
agrupa por tabela de destino. CAPADMISSION tem 34 FKs saindo para 18 tabelas
distintas -- os dois números estão certos e medem coisas diferentes. Cada item
da lista traz `fks`, `colunas` e, quando a IA escreveu, o `motivo` da ligação
em português.

`total_encontrado` diz quantas casaram antes do limite; quando for maior que
`count`, vem um `aviso`. Sem isso, 25 linhas de volta podem ser o resultado
inteiro ou a ponta de 400.

Não custa chamada de embedding.
""")
def list_catalog_tables(
    termo: str = "",
    tem_dependencias: Optional[bool] = None,
    tem_dependentes: Optional[bool] = None,
    max_n_registros: Optional[int] = None,
    dependencias: bool = False,
    dependentes: bool = False,
    campos: bool = False,
    limite: int = listagem.LIMITE_PADRAO,
):
    try:
        return listagem.listar(
            termo,
            tem_dependencias=tem_dependencias,
            tem_dependentes=tem_dependentes,
            max_n_registros=max_n_registros,
            dependencias=dependencias,
            dependentes=dependentes,
            campos=campos,
            limite=limite,
        )
    except catalogo.ConsultaFalhou as falha:
        return _erro(falha)


@mcp.tool(description="""
Lista CAMPOS cujo nome ou descrição contenha um texto, com a tabela e o grau de
ligação de cada um.

É o par de `list_catalog_tables`, no nível da coluna. Use para "onde tem um
campo de CPF", "que campos guardam data de nascimento", "quais colunas se
chamam IDPATIENT" -- quando o alvo é o campo, não a tabela.

Diferença para `search_fields`: lá é vetor, devolve os 20 campos mais parecidos
com um conceito e custa uma chamada paga. Aqui é LIKE, devolve TODOS os que
casam o texto e não custa nada. Para "quais" e "quantos", use esta.

Parâmetros:
- termo: varre o nome da coluna E a descrição gerada por IA no mesmo parâmetro,
  com acento ignorado dos dois lados -- "data de nascimento" acha BIRTHDAY pela
  descrição, "IDPATIENT" acha pelo nome. Vazio lista tudo.
- limite: máximo de campos (padrão 25, teto 100).

Retorna, por linha, a linha inteira do campo mais o contexto da tabela:
- tabela, tabela_rotulo, tabela_registros -- para saber se a coluna está numa
  tabela viva ou num snapshot vazio;
- posicao, campo, tipo, tipo_completo, aceita_nulo, eh_pk, na_amostra, descricao,
  e tamanho/precisao/escala/valor_default/comentario quando existirem (nulo em
  coluna numérica quer dizer "não se aplica", então sai da linha);
- dependencias: quantas FKs SAEM deste campo (ele aponta para outra tabela);
- dependentes: quantas FKs ENTRAM neste campo (outras tabelas apontam para ele).

Os dois números localizam o campo no grafo: `dependentes` alto é chave que o
schema inteiro referencia; `dependencias` 1 é uma FK comum; os dois em 0 é campo
de dado, sem ligação. Para ver QUAIS são as ligações, use `field_relationships`.

`total_encontrado` diz quantos casaram antes do limite.

Não custa chamada de embedding.
""")
def list_catalog_fields(termo: str = "", limite: int = listagem.LIMITE_PADRAO):
    try:
        return listagem.listar_campos(termo, limite=limite)
    except catalogo.ConsultaFalhou as falha:
        return _erro(falha)


@mcp.tool(description="""
Mostra as FKs que saem e que entram num CAMPO específico.

É o detalhe do que `list_catalog_fields` só conta. Use quando precisar saber
com o que uma coluna se liga: para montar um JOIN, para entender o que quebra ao
mexer nela, ou para achar a tabela-mestre por trás de um ID.

Parâmetros:
- campo: nome da coluna (case-insensitive). Obrigatório.
- tabela: restringe àquela tabela. Omita para ver o campo no schema inteiro --
  é a resposta para "quem depende de IDPATIENT, em qualquer lugar", já que o
  mesmo nome de coluna se repete pelo schema.
- limite: máximo de tabelas listadas em `aparece_em` (padrão 25, teto 100). Não
  corta as FKs: as listas de dependência vêm completas.

Retorna:
- aparece_em: em que tabelas existe uma coluna com esse nome, com posicao,
  tipo_completo, eh_pk, descricao e o nº de registros da tabela;
- dependencias: FKs que SAEM do campo. Cada uma traz a tabela de origem, o
  constraint_nome, `aponta_para_tabela` e `aponta_para_campo` (a coluna
  emparelhada do outro lado), as colunas_da_fk e o delete_rule;
- dependentes: FKs que ENTRAM no campo, com `apontado_por_tabela` e
  `apontado_por_campo`;
- count: tabelas_com_o_campo, tabelas_listadas, dependencias e dependentes.

Em FK composta as colunas são emparelhadas por posição, então
`aponta_para_campo` é a coluna certa do outro lado, não a primeira da lista --
`colunas_da_fk` mostra o conjunto inteiro da constraint.

Leitura típica: chave primária vem com `dependencias: []` e uma pilha de
dependentes; uma FK vem com uma dependência e nenhum dependente.

Não custa chamada de embedding.
""")
def field_relationships(
    campo: str,
    tabela: str = "",
    limite: int = listagem.LIMITE_PADRAO,
):
    try:
        return listagem.relacoes_do_campo(campo, tabela=tabela, limite=limite)
    except (listagem.CampoNaoEncontrado, catalogo.ConsultaFalhou) as falha:
        return _erro(falha)
    except ValueError as falha:
        return {"erro": str(falha), "detalhe": ""}


@mcp.tool(description="""
Retorna tudo que o catálogo sabe sobre UMA tabela: descrição de IA, colunas com
tipo e descrição, chaves, índices, quem a referencia e, se pedido, amostra.

É o passo seguinte a `search_tables`: a busca diz QUAL tabela; esta tool diz o
que tem dentro dela. Use antes de escrever qualquer SQL contra a origem.

Parâmetros:
- nome: nome da tabela. Não precisa acertar a caixa -- "capadmission" serve.
- colunas: inclui a lista de colunas (padrão true). Passe false quando quiser só
  a visão geral: descrição, chaves, índices e relacionamentos, sem o dicionário.
  Em CAPADMISSION (194 colunas) isso é ~5,5 mil tokens em vez de ~15 mil.
- filtro_coluna: mostra só as colunas cujo nome, comentário ou descrição contêm
  este texto. Feito para tabelas largas -- a mediana do catálogo é 8 colunas,
  mas 27 tabelas passam de 200 e uma tem 1.000. Ex: "paciente", "data", "valor".
  Acima de 200 colunas a lista vem truncada por posição, e `estrutura` avisa em
  `colunas_omitidas`; nesse caso use este filtro para chegar ao resto.
- amostra: quantas linhas de exemplo trazer (0 por padrão, máximo 10). A linha
  vem inteira, nulos inclusive. A máscara da extração é parcial e este schema é
  em inglês, então pode vir identificação em claro -- veja `sample_table_data`,
  que é a tool própria para amostra e aceita escolher colunas.

Retorna:
- tabela: nome, tipo, comentário da origem, num_registros, tem_registros,
  particionada e quando foi mapeada.
- descricao: o que a IA escreveu -- rotulo, dominio, resumo, funcao, e as listas
  `dependencias` (de que tabelas esta depende, e por quê) e `dependentes` (quem
  depende dela). Traz `desatualizada: true` quando a estrutura mudou depois de a
  descrição ter sido escrita -- nesse caso confie nas colunas, não no texto.
  Vem null quando a tabela ainda não foi descrita.
- estrutura: as contagens (colunas, restrições, índices, FKs entrando e saindo).
- colunas: pos, nome, tipo, nulo, descricao, e -- só quando existem -- pk, fk
  (no formato TABELA.COLUNA), default e comentario.
- chaves: primaria, unicas e estrangeiras (com coluna, referência e delete_rule).
- indices: nome, unico, tipo e colunas.
- referenciada_por: que tabelas apontam para esta, com quantas FKs e por quais
  colunas. É como se descobre o que dá JOIN a partir daqui.

Para planejar um JOIN, leia `chaves.estrangeiras` (para onde esta tabela aponta)
e `referenciada_por` (quem aponta para ela). As listas `dependencias` e
`dependentes` da descrição explicam o porquê de cada ligação em português.

Peso da resposta, medido: a tabela mediana do catálogo tem 8 colunas e cabe em
~700 tokens. Só as largas pesam -- CAPADMISSION dá ~15 mil tokens completa.
Numa tabela larga, prefira `colunas=false` para decidir se é ela mesmo, e só
então volte com `filtro_coluna`.

Não custa chamada de embedding.
""")
def describe_catalog_table(
    nome: str,
    colunas: bool = True,
    filtro_coluna: str = "",
    amostra: int = 0,
):
    try:
        return objeto.detalhar(
            nome, colunas=colunas, amostra=amostra, filtro_coluna=filtro_coluna
        )
    except (objeto.ObjetoNaoEncontrado, catalogo.ConsultaFalhou) as falha:
        return _erro(falha)


@mcp.tool(description="""
Traz linhas de exemplo de uma tabela, para ver o formato e o domínio dos valores.

LEIA ISTO ANTES DE USAR OS DADOS. A amostra NÃO é uma consulta à origem: é o
retrato que a extração do catálogo guardou. Três consequências:

- É datada. Foi coletada uma vez, não reflete mudanças posteriores.
- São no máximo 20 linhas, sempre as primeiras que a extração leu. Não é uma
  seleção aleatória e não aceita filtro, então NÃO dá para concluir nada sobre
  distribuição, frequência ou totais. Cinco linhas com STATUS=4 numa tabela de
  31.560 registros não querem dizer que STATUS seja sempre 4.
- Pode conter dado pessoal em claro. A extração mascara por nome de coluna com
  uma lista de padrões em português, e este schema é em inglês, então boa parte
  passa sem máscara: GLBPERSON.NAME traz nome completo de pessoa, ADDRESS traz
  logradouro e TELEPHONE traz telefone. Use os valores para entender o formato
  do campo; não repita identificação numa resposta nem a carregue adiante.

Use para: entender o formato de um campo, ver que valores um código assume na
prática, confirmar se uma coluna é usada, sanity-check antes de escrever SQL.
Não use para: contar, somar, medir frequência ou afirmar o estado atual do dado.

Parâmetros:
- nome: nome da tabela (case-insensitive).
- linhas: quantas trazer (padrão 5, máximo 20 -- que é tudo o que existe).
- colunas: lista separada por vírgula para trazer só essas. Ex: "ID,STATUS,
  DIAGNOSTIC". A linha vem inteira, com as colunas nulas inclusive, então em
  tabela larga esta é a forma de não gastar contexto à toa: CAPADMISSION tem
  194 colunas por linha e a maioria dos valores é nula.

Retorna:
- tabela, origem (schema, service, quando foi extraída, modo de máscara),
  num_registros_na_tabela e linhas_disponiveis -- para dimensionar o que a
  amostra representa;
- linhas: cada uma com o número da linha e os dados, com as colunas na ordem da
  tabela (a mesma de `describe_catalog_table`, para ler as duas lado a lado);
- colunas_fora_da_amostra e, quando `colunas` pedir nomes que não existem,
  colunas_inexistentes.

`colunas_fora_da_amostra` são colunas que a extração nunca coletou pelo tipo
(LONG, BLOB, XMLTYPE...). Elas existem na tabela e não aparecem em nenhuma
linha aqui -- GLBHISTORYTEXT tem 2 colunas e a amostra mostra 1.

Tabela vazia na extração devolve `linhas: []` com um aviso, não um erro.

Não custa chamada de embedding.
""")
def sample_table_data(
    nome: str,
    linhas: int = 5,
    colunas: str = "",
):
    try:
        return objeto.amostrar(nome, linhas=linhas, colunas=colunas)
    except (objeto.ObjetoNaoEncontrado, catalogo.ConsultaFalhou) as falha:
        return _erro(falha)


@mcp.tool(description="""
Mostra quanto do catálogo está vetorizado e com qual modelo de embedding.

Use para diagnosticar uma busca que volta vazia ou fraca: se o número de tabelas
e campos aqui for muito menor do que o catálogo real, a descrição de IA ainda não
foi gerada para boa parte dele e as buscas só enxergam o que está listado.

Não chama API de embedding e não custa nada.

Retorna uma linha por modelo, com quantas tabelas e quantos campos têm vetor.
""")
def vector_coverage():
    try:
        return {"rows": busca.cobertura()}
    except catalogo.ConsultaFalhou as falha:
        return _erro(falha)


# ----------------------------
# 🗄️ Consulta ao Oracle de origem
# ----------------------------

@mcp.tool(description="""
Executa um SELECT no Oracle de ORIGEM e devolve as linhas em JSON ou em tabela
Markdown.

É a única tool que toca o dado vivo. Todas as outras leem o CATÁLOGO, que é
metadado: `search_tables` diz onde o dado mora, `describe_catalog_table` diz que
colunas existem, `sample_table_data` mostra um retrato datado de até 20 linhas.
Para responder "quantos", "qual o total", "quais os de ontem", é aqui.

Ordem de trabalho que evita SQL errada: descubra a tabela com `search_tables`,
confirme as colunas e as FKs com `describe_catalog_table`, e só então escreva a
consulta. Os nomes deste schema são abreviados e em inglês -- chutar nome de
coluna custa uma ida e volta a mais do que conferir no catálogo.

Parâmetros:
- sql: uma sentença SELECT (ou WITH ... SELECT). Sem ponto e vírgula no fim,
  sem lote de sentenças. Não precisa qualificar o schema: a sessão já abre com
  CURRENT_SCHEMA no schema do negócio, então `FROM CAPADMISSION` basta.
- limite: teto de linhas (padrão 100, máximo 1000).
- formato: "json" (padrão) ou "markdown".

Sobre o teto: ele é sempre aplicado, e por fora da sua consulta -- ela vira
subquery de um ROWNUM. Não adianta escrever FETCH FIRST 5000 ROWS, nem omitir
qualquer limite: o corte acontece de todo jeito. Quando corta, vem
`truncado: true` e um `aviso`. Nesse caso NÃO conclua total, média ou "não
existe mais nenhum" a partir das linhas que voltaram; refaça a pergunta como
COUNT(*), SUM() ou GROUP BY, que respondem em uma linha só e não esbarram no
teto.

Escolha do formato: "markdown" para mostrar o resultado a uma pessoa -- várias
colunas por várias linhas em JSON gastam o dobro dos tokens e não se leem. Use
"json" quando for encadear o resultado em outra chamada ou fazer conta em cima.

Somente leitura, em duas camadas: INSERT, UPDATE, DELETE, MERGE, DDL e bloco
PL/SQL são recusados antes de sair daqui, e a sessão no Oracle abre como READ
ONLY. Não há como escrever por esta tool.

Retorna:
- em json: colunas, rows (uma lista de objetos coluna->valor) e count;
- em markdown: markdown (a tabela pronta), colunas e count;
- nos dois: limite_aplicado, truncado, schema, segundos e sql_executada.

Cuidados com o valor: texto acima de 500 caracteres vem truncado com aviso no
próprio valor, e BLOB não trafega -- vira `<BINARIO n bytes...>`. Este schema é
clínico e está em inglês: NAME, ADDRESS e TELEPHONE trazem dado pessoal em
claro. Use para responder a pergunta; não repita identificação desnecessária na
resposta.

O banco fica em rede interna. Sem VPN o erro é de conexão, não de SQL --
`oracle_status` diz qual dos dois é.
""")
def run_oracle_query(
    sql: str,
    limite: int = oracle.LIMITE_PADRAO,
    formato: str = "json",
):
    logging.info("run_oracle_query (limite=%s, formato=%s): %s", limite, formato, sql)
    try:
        return oracle.executar(sql, limite=limite, formato=formato)
    except (oracle.ConsultaBloqueada, oracle.OracleIndisponivel,
            oracle.ExecucaoFalhou) as falha:
        return _erro(falha)


@mcp.tool(description="""
Testa a conexão com o Oracle de origem e diz com quem se está falando.

Use quando `run_oracle_query` falhar, para separar problema de rede de problema
de SQL: se esta tool também falhar, o banco está inalcançável (a VPN é a causa
usual, já que ele fica em rede interna) e não adianta reescrever a consulta.

Retorna host, usuário da sessão, schema corrente, nome do banco, versão do
Oracle, a hora do servidor e os limites de linha em vigor.
""")
def oracle_status():
    try:
        return oracle.status()
    except oracle.OracleIndisponivel as falha:
        return _erro(falha)


# ----------------------------
# ✍️ Escrita no dicionário (via opalus-data-connect)
# ----------------------------

@mcp.tool(description="""
Grava o rótulo e a descrição de uma TABELA no catálogo.

O texto não é gravado por este servidor: vai para a API do opalus-data-connect,
que persiste E refaz o vetor de busca daquela tabela na mesma chamada. Por isso a
correção já aparece na próxima `search_tables` -- a resposta traz `vetores` com o
que foi revetorizado.

O que o corpo não traz NÃO muda. Mandar só `rotulo` deixa resumo, função e
domínio como estão. Não dá para apagar por aqui: texto em branco é recusado antes
de sair (para limpar, use a tela do opalus-data-connect).

Quando usar: a descrição que voltou de `describe_catalog_table` está vaga
("tabela de controle"), errada, ou a tabela ainda não tem descrição nenhuma.

Antes de escrever, olhe o dado. `describe_catalog_table` dá a estrutura e as FKs,
`sample_table_data` dá o formato dos valores e `run_oracle_query` responde o resto
(que valores STATUS assume de verdade, se a tabela ainda recebe registro este
ano). Descrição de dicionário é afirmação sobre o banco: escreva o que conferiu.

Parâmetros -- informe ao menos um dos quatro textos:
- tabela: nome como está no catálogo. A caixa importa (no Oracle é MAIÚSCULA).
- resumo: o que a tabela guarda, em português corrente. Uma a três frases.
  É o texto principal, e é obrigatório na PRIMEIRA vez que a tabela é descrita --
  sem ele não há o que vetorizar, e a API recusa.
- rotulo: a legenda curta de listagem, até 5 palavras ("Admissões de pacientes").
- funcao: que papel a tabela cumpre no sistema, quando não couber no resumo.
- dominio: a área de negócio ("internação", "faturamento").

Máximo de 2000 caracteres em resumo e função, 120 em rótulo e domínio.

Para descrever os CAMPOS, use `update_field_descriptions` -- e nesta ordem: a API
recusa campos de uma tabela que ainda não tem descrição própria.

Erros que valem ler: "Objeto desconhecido" é nome ou caixa errados (confira com
`describe_catalog_table`); "o texto foi gravado, mas o vetor não foi refeito"
quer dizer que o texto entrou e só o índice ficou velho -- não repita a chamada.
""")
def update_table_description(
    tabela: str,
    resumo: str = "",
    rotulo: str = "",
    funcao: str = "",
    dominio: str = "",
):
    logging.info("update_table_description: %s", tabela)
    try:
        return data_connect.atualizar_tabela(
            tabela, resumo=resumo, rotulo=rotulo, funcao=funcao, dominio=dominio
        )
    except (data_connect.TextoInvalido, data_connect.ConfiguracaoInvalida,
            data_connect.DataConnectIndisponivel, data_connect.EnvioRecusado) as falha:
        return _erro(falha)


@mcp.tool(description="""
Grava a descrição de um ou vários CAMPOS de uma tabela no dicionário de dados.

Vários de uma vez, e prefira assim: descrever 40 colunas numa chamada é uma
gravação e uma revetorização em lote do lado de lá, contra 40 de cada se for uma
por vez.

O que o corpo não traz NÃO muda: mandar 3 colunas não encosta nas outras 77 da
tabela. Descrição em branco é recusada aqui (do lado da API ela APAGARIA o texto)
-- a coluna sai da chamada e volta listada em `ignorados_por_estarem_em_branco`.

Parâmetros:
- tabela: nome da tabela. A caixa importa.
- campos: objeto {"COLUNA": "descrição", ...}. O nome da coluna tem de bater com
  o catálogo, inclusive na caixa -- no Oracle os nomes estão em MAIÚSCULAS, e uma
  coluna que não existe faz a chamada inteira voltar com "Coluna inexistente".
  Até 500 por chamada.

O que faz uma boa descrição de campo, neste catálogo: diga o que o valor significa
e, quando for código, o que os valores assumem na prática ("1=ativo, 2=cancelado
-- conferido em 31.560 registros"). Nome de coluna aqui é críptico e em inglês; a
descrição é o que está em português, e é por ela que a busca semântica encontra o
campo. Confirme com `sample_table_data` ou `run_oracle_query` antes de afirmar.

A tabela precisa ter descrição própria ANTES dos campos -- se não tiver, a API
recusa e manda usar `update_table_description` primeiro.

Máximo de 2000 caracteres por descrição.
""")
def update_field_descriptions(tabela: str, campos: dict[str, str]):
    logging.info("update_field_descriptions: %s (%s campos)", tabela, len(campos or {}))
    try:
        return data_connect.atualizar_campos(tabela, campos)
    except (data_connect.TextoInvalido, data_connect.ConfiguracaoInvalida,
            data_connect.DataConnectIndisponivel, data_connect.EnvioRecusado) as falha:
        return _erro(falha)


# ----------------------------
# 📊 Faturamento, glosa e auditoria
# ----------------------------
# As 15 tools e suas descrições moram em src/tools_faturamento.py, e a SQL em
# src/faturamento.py. `_erro` vai como parâmetro para o módulo não precisar
# importar este arquivo de volta.
tools_faturamento.registrar(mcp, _erro)


# ----------------------------
# 🛠️ Inspeção técnica (último recurso)
# ----------------------------
# @mcp.tool(description="""
# Ferramenta de último recurso para executar SQL de leitura no banco do catálogo.

# Atenção ao escopo: este banco guarda o CATÁLOGO (metadados das tabelas), não os
# dados de negócio. Consultar aqui responde "que tabelas existem", não "quantos
# pacientes internaram".

# Use apenas quando:
# - `search_catalog` não conseguir responder;
# - for necessário diagnóstico técnico ou inspeção de metadados não expostos.

# Não use para:
# - localizar tabela ou campo por assunto, pois `search_catalog` faz isso melhor;
# - qualquer operação de escrita.

# Regras:
# - apenas consultas SELECT simples são permitidas;
# - INSERT, UPDATE, DELETE, DROP, ALTER e TRUNCATE são bloqueados;
# - limite padrão de retorno: 100 linhas;
# - as tabelas ficam no schema `catalogo`; qualifique o nome (catalogo.objeto).

# Retorna columns, rows e count.
# """)
# def run_sql(query: str, limit: int = 100):
#     logging.info("Executando query: %s", query)
#     return views.run_sql(query=query, limit=limit)


# @mcp.tool(description="""Ferramenta de último recurso. Lista as tabelas do schema `catalogo` (o catálogo em si, não as tabelas mapeadas). Retorna uma lista de strings.""")
# def list_tables():
#     return views.list_tables()


# @mcp.tool(description="""Ferramenta de último recurso. Retorna as colunas e tipos de dados de uma tabela do banco do catálogo. Retorna uma lista de pares [column_name, data_type].""")
# def describe_table(table_name: str):
#     return views.describe_table(table_name=table_name)


if __name__ == "__main__":
    # stdio (default) mantém o fluxo do mcpo/OpenWebUI intacto.
    # MCP_TRANSPORT=streamable-http expõe o endpoint MCP nativo para o Claude Code.
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        mcp.settings.host = os.getenv("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.getenv("MCP_PORT", "8002"))
    mcp.run(transport=transport)
