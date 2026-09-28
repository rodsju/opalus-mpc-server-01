"""As 15 tools de faturamento, glosa e auditoria.

Mora fora do src/main.py por volume: as descrições destas tools são o grosso do
texto, e o main.py já carrega 13 tools do catálogo. `registrar(mcp, erro)` recebe
o servidor FastMCP e a função que transforma exceção em payload -- a mesma do
main.py, passada como parâmetro para não haver import circular.

A SQL não está aqui: está em src/faturamento.py, com o comentário de por que cada
consulta é do jeito que é. Aqui ficam só a assinatura, a descrição que o agente lê
e a tradução de exceção em resposta.

Sobre o tamanho das descrições: elas repetem pouco de propósito. A convenção de
período e de unidade está explicada UMA vez, nas instruções do servidor
(src/main.py), e cada tool só diz o que é próprio dela. Quinze cópias de vinte
linhas de convenção seriam ~4 mil tokens gastos em todo boot de agente.
"""

from . import faturamento, oracle

# Toda falha esperada destas tools: período malformado, consulta recusada, banco
# fora do ar, SQL que o Oracle não aceitou.
FALHAS = (
    faturamento.PeriodoInvalido,
    oracle.ConsultaBloqueada,
    oracle.OracleIndisponivel,
    oracle.ExecucaoFalhou,
)

# Trecho comum no fim de cada descrição com recorte de tempo. É curto porque a
# explicação longa está nas instruções do servidor.
RECORTE = """
Período (obrigatório): `mes="AAAA-MM"` para um mês fechado, ou o par `dt_ini`/
`dt_fim` em AAAA-MM-DD para recorte livre -- e nesse caso o FIM É EXCLUSIVO.
Unidade: opcional; em branco traz todas. Ignora caixa e espaço sobrando.
`limite` (padrão 100, teto 1000) e `formato` ("json" ou "markdown") como nas
demais tools."""


def registrar(mcp, erro):
    """Registra as 15 tools no servidor. Chamado uma vez, pelo src/main.py."""

    def _com_periodo(consulta, mes, dt_ini, dt_fim, unidade, limite, formato, **extra):
        try:
            inicio, fim = faturamento.periodo(mes, dt_ini, dt_fim)
            return consulta(
                inicio, fim, unidade=unidade, limite=limite, formato=formato, **extra
            )
        except FALHAS as falha:
            return erro(falha)

    # ------------------------------------------------------------------
    # Dimensões
    # ------------------------------------------------------------------

    @mcp.tool(description="""
Lista as unidades de negócio do grupo. Comece por aqui antes de filtrar qualquer
relatório por unidade.

Existem DUAS dimensões de unidade neste ERP, e os rótulos não são
intercambiáveis. Passar o nome de uma no filtro da outra devolve zero linhas:

- escopo="faturam" (padrão): as empresas que aparecem no faturamento
  (CAPPAYMENT.IDHOMECARE). São estes nomes que valem em TODA tool de
  faturamento, glosa e contas -- 'Premier Brooklin', 'PLENO SAUDE - RJ'...
- escopo="canonica": a dimensão GLBHEALTHPROVIDER, que é a que vale nas tools de
  ORÇAMENTO -- 'Premier Brooklin HSP', 'Premier Brooklin AMB', 'PLENO RJ HC'.
  Traz também o tipo de assistência e se está inativa.
- escopo="mapa": a correspondência entre as duas, uma linha por empresa com os
  health providers dela. É a tool para responder "o orçamento de qual unidade
  vira o faturamento desta aqui".

Sem período: é cadastro, não movimento.
""")
    def list_business_units(escopo: str = "faturam", limite: int = 100, formato: str = "json"):
        try:
            return faturamento.unidades(escopo, limite=limite, formato=formato)
        except FALHAS as falha:
            return erro(falha)

    @mcp.tool(description="""
Lista as operadoras de saúde (fontes pagadoras) cadastradas.

- apenas_com_faturamento=true (padrão): só as que têm conta contra o grupo. É a
  lista útil, e é curta.
- false: todas as cadastradas com HEALTHPLAN=1, inclusive as que nunca faturaram.

Use para saber o nome exato antes de ler um relatório por operadora. Conta sem
operadora aparece nas outras tools como '(particular/paciente)' -- é atendimento
particular, não erro de cadastro.

Sem período: é cadastro, não movimento.
""")
    def list_health_plans(
        apenas_com_faturamento: bool = True, limite: int = 100, formato: str = "json"
    ):
        try:
            return faturamento.operadoras(
                apenas_com_faturamento, limite=limite, formato=formato
            )
        except FALHAS as falha:
            return erro(falha)

    # ------------------------------------------------------------------
    # Faturamento
    # ------------------------------------------------------------------

    @mcp.tool(description="""
Contas de faturamento do período, agrupadas por unidade e situação, com contagem
e valor.

É a visão de abertura de qualquer conversa sobre faturamento: quanto foi faturado
no mês, em que estágio do fluxo cada conta está e onde está parado.

As situações são exclusivas e nesta ordem de precedência: Simulação (ensaio, NÃO
é faturamento real) > Baixada (write-off) > Aberta (não liberada) > Liberada (não
exportada) > Exportada (fechada).

Atenção ao somar: a linha de 'Simulação' não é receita. Para números de negócio,
use as tools que já excluem simulação (`billing_by_health_plan`,
`denials_by_unit`) ou desconte essa linha.

O valor segue a regra do ERP: item de imposto (RESOURCETYPE 15) entra pelo custo,
o resto pelo preço de venda. Isso já está na consulta.

CONTA SEM ITEM NÃO APARECE AQUI. A consulta soma itens, então conta aberta e
ainda vazia fica de fora -- em junho/2026 são 399 de 1.203. Se o usuário
perguntar "quantas contas existem no mês", este número é menor que o do sistema;
diga que aqui se contam as contas COM lançamento.
""" + RECORTE)
    def billing_invoices_by_status(
        mes: str = "", dt_ini: str = "", dt_fim: str = "", unidade: str = "",
        limite: int = 100, formato: str = "json",
    ):
        return _com_periodo(
            faturamento.contas_por_status, mes, dt_ini, dt_fim, unidade, limite, formato
        )

    @mcp.tool(description="""
Contas de faturamento do período, uma linha por conta, com unidade, operadora,
situação, autorização e valor.

É o detalhe por trás de `billing_invoices_by_status` -- use quando a pergunta for
sobre contas específicas ("as cinco maiores de julho", "quais contas da Amil
seguem abertas"), não sobre totais.

Vem ordenado por valor decrescente, então as maiores aparecem primeiro mesmo com
o limite baixo. Uma unidade-mês costuma passar de 100 contas: se vier
`truncado: true`, NÃO conclua totais a partir daqui -- volte na versão agrupada.

`autorizacao_externa` (AUTHORIZEXTCODE) está vazia na maioria das contas deste
banco; ausência ali não quer dizer conta sem autorização.

Conta sem nenhum item não aparece (399 de 1.203 em junho/2026): a consulta parte
dos itens.
""" + RECORTE)
    def billing_invoices_detail(
        mes: str = "", dt_ini: str = "", dt_fim: str = "", unidade: str = "",
        limite: int = 100, formato: str = "json",
    ):
        return _com_periodo(
            faturamento.contas_detalhe, mes, dt_ini, dt_fim, unidade, limite, formato
        )

    @mcp.tool(description="""
Faturamento do período por unidade e operadora, com número de contas e valor.

É o relatório de "a receber, por fonte pagadora": quanto cada operadora deve ao
grupo no período. Exclui simulação, então o número é de negócio.

Para o mesmo recorte visto por situação do processo, use
`billing_invoices_by_status`; para o que está parado no fluxo,
`open_invoices_by_health_plan`.
""" + RECORTE)
    def billing_by_health_plan(
        mes: str = "", dt_ini: str = "", dt_fim: str = "", unidade: str = "",
        limite: int = 100, formato: str = "json",
    ):
        return _com_periodo(
            faturamento.faturamento_por_operadora, mes, dt_ini, dt_fim, unidade,
            limite, formato,
        )

    @mcp.tool(description="""
Contas em aberto no período, por operadora: o que ainda não fechou o fluxo.

"Em aberto" aqui é processo, não inadimplência: conta real (não simulação), não
baixada, e que ainda não foi liberada ou ainda não foi exportada. É dinheiro
parado entre o atendimento e a cobrança -- e o gargalo costuma estar numa
operadora só.

Não confunda com `budget_billing_gap`, que é o que nem virou conta ainda.
""" + RECORTE)
    def open_invoices_by_health_plan(
        mes: str = "", dt_ini: str = "", dt_fim: str = "", unidade: str = "",
        limite: int = 100, formato: str = "json",
    ):
        return _com_periodo(
            faturamento.contas_em_aberto, mes, dt_ini, dt_fim, unidade, limite, formato
        )

    # ------------------------------------------------------------------
    # Orçamento
    # ------------------------------------------------------------------

    @mcp.tool(description="""
Orçamentos do período agrupados por unidade e situação, com contagem e valor
previsto.

ATENÇÃO À UNIDADE: aqui ela vem da dimensão canônica (GLBHEALTHPROVIDER, via a
admissão), e os nomes são OUTROS -- 'Premier Brooklin HSP', não
'Premier Brooklin'. Use `list_business_units(escopo="canonica")` para os nomes
válidos, ou "mapa" para a correspondência. O filtro também aceita o nome da
empresa, quando você só tiver esse.

LEIA ANTES DE CONCLUIR: a maioria dos orçamentos não tem valor preenchido --
medido em junho/2026, 696 de 3.397. `valor_previsto` baixo quer dizer campo
vazio, não orçamento pequeno. Para contar volume, use a coluna `orcamentos`; o
valor só é confiável no recorte que tem BUDGETCHARGE.

Situações, por precedência: Cancelado > Autorizado (operadora) > Autorizado
(interno) > Liberado > Pendente.
""" + RECORTE)
    def budgets_by_status(
        mes: str = "", dt_ini: str = "", dt_fim: str = "", unidade: str = "",
        limite: int = 100, formato: str = "json",
    ):
        return _com_periodo(
            faturamento.orcamentos_por_status, mes, dt_ini, dt_fim, unidade,
            limite, formato,
        )

    @mcp.tool(description="""
Orçamentos do período, um por linha, com senha de autorização, valor e
observações.

Detalhe por trás de `budgets_by_status`. Use para investigar casos ("quais
orçamentos da unidade X estão pendentes há mais tempo"), não para totalizar.

Unidade: dimensão canônica (GLBHEALTHPROVIDER) -- veja
`list_business_units(escopo="canonica")`.

Ordenado por data de início decrescente. Volume alto: uma unidade-mês passa de
mil orçamentos, então espere `truncado: true` e estreite o recorte.
""" + RECORTE)
    def budgets_detail(
        mes: str = "", dt_ini: str = "", dt_fim: str = "", unidade: str = "",
        limite: int = 100, formato: str = "json",
    ):
        return _com_periodo(
            faturamento.orcamentos_detalhe, mes, dt_ini, dt_fim, unidade, limite, formato
        )

    @mcp.tool(description="""
Orçamentos AUTORIZADOS pela operadora que não geraram nenhum item de faturamento.

É a métrica "faturei tudo o que eu devia?". Cada linha é receita autorizada que
não virou conta -- o vazamento mais caro e mais silencioso do ciclo, porque nada
no sistema reclama de uma autorização que ninguém faturou.

Só entra o que foi autorizado pela operadora (AUTHORIZEXTSTATUS=1) e não foi
cancelado. Orçamento pendente ou recusado não é vazamento, é fluxo normal.

- detalhado=false (padrão): uma linha por unidade, com a contagem e o valor
  pendente. Comece por aqui.
- detalhado=true: um orçamento por linha, para ir atrás dos casos.

Unidade: dimensão canônica (GLBHEALTHPROVIDER).

O valor pendente sofre da mesma lacuna de `budgets_by_status`: boa parte dos
orçamentos não tem BUDGETCHARGE, então a CONTAGEM é o número firme e o valor é
piso, não total.
""" + RECORTE)
    def budget_billing_gap(
        mes: str = "", dt_ini: str = "", dt_fim: str = "", unidade: str = "",
        detalhado: bool = False, limite: int = 100, formato: str = "json",
    ):
        return _com_periodo(
            faturamento.gap_orcamento_faturamento, mes, dt_ini, dt_fim, unidade,
            limite, formato, detalhado=detalhado,
        )

    # ------------------------------------------------------------------
    # Glosa
    # ------------------------------------------------------------------

    @mcp.tool(description="""
Glosa por unidade no período: faturado, glosado, sustentado, recuperado e a taxa
de glosa em %.

É a visão de abertura de qualquer conversa sobre glosa. Traz o denominador junto
(o faturado), então a taxa já vem calculada e comparável entre unidades.

Leia as três colunas de glosa juntas: `glosa` é o que a operadora cortou,
`glosa_sustentada` é o que ela manteve depois do recurso, `glosa_recuperada` é o
que voltou. Sustentada alta é problema de processo nosso; recuperada alta quer
dizer que o recurso funciona e vale insistir.

QUANDO A GLOSA PARECER BAIXA DEMAIS, olhe a data. A glosa chega MESES depois da
competência, porque depende da auditoria da operadora. Medido neste banco:
junho/2026 tem 1 item glosado em 102 mil; julho/2025 tem 302. Um mês recente com
glosa quase zero não é boa notícia -- é auditoria que ainda não aconteceu. Para
avaliar glosa de verdade, use competências com pelo menos uns seis meses.

Só contas reais: simulação fica de fora.
""" + RECORTE)
    def denials_by_unit(
        mes: str = "", dt_ini: str = "", dt_fim: str = "", unidade: str = "",
        limite: int = 100, formato: str = "json",
    ):
        return _com_periodo(
            faturamento.glosa_por_unidade, mes, dt_ini, dt_fim, unidade, limite, formato
        )

    @mcp.tool(description="""
Glosa por operadora no período: quem glosa, quanto, e quanto se recupera de cada
uma.

É o par de `denials_by_unit` pelo outro eixo. A pergunta que ela responde é de
negociação: qual operadora concentra a glosa, e com qual delas o recurso costuma
dar certo.

Só itens efetivamente glosados (AUDITBILLVALUE > 0) e contas reais (sem
simulação). O filtro `unidade` continua sendo o da empresa de faturamento, não o
da operadora.

Vale a mesma ressalva de prazo: glosa de competência recente ainda não foi
auditada. Veja `denials_by_unit`.
""" + RECORTE)
    def denials_by_health_plan(
        mes: str = "", dt_ini: str = "", dt_fim: str = "", unidade: str = "",
        limite: int = 100, formato: str = "json",
    ):
        return _com_periodo(
            faturamento.glosa_por_operadora, mes, dt_ini, dt_fim, unidade, limite, formato
        )

    @mcp.tool(description="""
Glosas item a item, com categoria, recurso, quantidade, valores e o motivo
escrito pela operadora.

É a tool de conferência: quando o agregado apontar um problema, é aqui que se vê
o caso concreto. Ordenado por valor de glosa decrescente.

O que cada coluna entrega, medido em 2025 (3.145 itens glosados):
- `categoria_recurso` e `recurso`: o que foi glosado, em português
  ("Clas. Profissionais / Fisioterapia motora", "Equipamentos / Cadeira De Rodas").
- `qtd_glosada` (3.140 preenchidos): a quantidade glosada, par do valor.
- `status` e `cod_recurso`: preenchidos em ~95% dos itens.
- `motivo_texto`: a narrativa da operadora, preenchida em ~67%.
- `cod_glosa_tiss`: HOJE SEMPRE VAZIA neste banco (0 de 3.145). Está na saída
  porque o dia em que o TISS passar a ser gravado ela aparece sozinha -- mas não
  monte análise em cima dela.

Para contar ou somar por motivo, não leia linha a linha: use
`denial_reasons_taxonomy`.
""" + RECORTE)
    def denials_detail(
        mes: str = "", dt_ini: str = "", dt_fim: str = "", unidade: str = "",
        limite: int = 100, formato: str = "json",
    ):
        return _com_periodo(
            faturamento.glosa_detalhe, mes, dt_ini, dt_fim, unidade, limite, formato
        )

    @mcp.tool(description="""
Glosa agrupada por MOTIVO, com itens e valor -- a pergunta "por que estamos sendo
glosados".

As categorias: Sem narrativa, Data de cadastro indevida, Autorização tardia
(senha pós-início), Divergência de senha/autorização, Divergência de código,
Unidade de medida, Período de validade, Visitas/concessão,
Orçamento/prorrogação, Contrato/negociação, Liminar judicial,
Documentação/autorização em anexo, Outros.

COMO A CLASSIFICAÇÃO É FEITA -- e por que isso importa: ela é heurística, por
busca de texto na narrativa que a operadora escreveu. Não é um campo do sistema.
Três consequências para a leitura:

1. 'SEM NARRATIVA' É A MAIOR CATEGORIA, e não é um motivo: é ausência dele. Na
   base inteira são R$ 8,56 mi, 48% da glosa. Entram aí o campo vazio e o
   placeholder -- a operadora que grava '.' ou ';' no lugar do texto. Glosa sem
   motivo escrito é glosa que ninguém tem como contestar, então esse número é
   achado operacional, não ruído de classificação: vale dizer isso ao usuário
   em vez de listar a categoria como mais uma linha.
2. 'Documentação/autorização em anexo' é a glosa em que a operadora pede papel
   (guia, fatura, documento). A regra roda DEPOIS das específicas de propósito:
   quando o texto diz "senha diverge, segue documentação em anexo", o motivo é a
   senha e o anexo é a instrução de recurso.
3. 'Outros' é o que não casou com nenhuma regra. Hoje são 19% do valor, e boa
   parte é texto NOSSO de recurso ("Glosa indevida", "Reapresentação",
   "RECURSADO"), não motivo da operadora. Se 'Outros' subir, leia
   `denial_reasons_raw` antes de concluir qualquer coisa.

Vale a ressalva de prazo de `denials_by_unit`: competência recente ainda não foi
auditada.
""" + RECORTE)
    def denial_reasons_taxonomy(
        mes: str = "", dt_ini: str = "", dt_fim: str = "", unidade: str = "",
        limite: int = 100, formato: str = "json",
    ):
        return _com_periodo(
            faturamento.motivos_taxonomia, mes, dt_ini, dt_fim, unidade, limite, formato
        )

    @mcp.tool(description="""
Os textos de motivo de glosa como a operadora escreveu, agrupados por texto
idêntico, com ocorrências e valor.

É a matéria-prima de `denial_reasons_taxonomy`. Use para dois fins: entender o
que caiu em 'Outros', e revisar se as regras da taxonomia ainda descrevem a
realidade -- a narrativa é texto livre e muda quando a operadora muda de sistema
ou de equipe.

Só itens com narrativa preenchida (~67% dos glosados em 2025), então o total
daqui é MENOR que o de `denial_reasons_taxonomy`, que conta também os sem texto.
""" + RECORTE)
    def denial_reasons_raw(
        mes: str = "", dt_ini: str = "", dt_fim: str = "", unidade: str = "",
        limite: int = 100, formato: str = "json",
    ):
        return _com_periodo(
            faturamento.motivos_texto, mes, dt_ini, dt_fim, unidade, limite, formato
        )

    # ------------------------------------------------------------------
    # Auditoria
    # ------------------------------------------------------------------

    @mcp.tool(description="""
Auditoria de contas por unidade: declarado, reconhecido e não reconhecido, na
ÚLTIMA execução de cada unidade.

Lê a TTMPAUDITBILLCTR, que é tabela de staging do processo de auditoria de
faturamento -- o confronto entre o que o grupo declarou e o que a operadora
reconheceu.

Legenda de invoice_status: 10 = declarado/em auditoria, 20 = reconhecido total,
30 = reconhecido parcial.

POR QUE O NÚMERO MUDA ENTRE UMA CONSULTA E OUTRA: esta tabela ACUMULA execuções
-- são 1,7 milhão de linhas para 13 unidades, em 5.575 execuções. Cada rodada do
processo grava tudo de novo com um TRANSKEY novo. Esta tool já prende no TRANSKEY
mais recente de cada unidade, que é a única leitura correta; somar sem esse filtro
multiplica os valores por um fator diferente em cada unidade. Se o resultado
mudar de uma semana para outra sem nada ter sido faturado, é porque rodou uma
auditoria nova.

Sem período: a tabela guarda o retrato da execução, não uma competência. O
recorte é só por unidade (a da empresa de faturamento).
""")
    def bill_audit_last_run(
        unidade: str = "", limite: int = 100, formato: str = "json"
    ):
        try:
            return faturamento.auditoria_ultima_execucao(
                unidade, limite=limite, formato=formato
            )
        except FALHAS as falha:
            return erro(falha)
