"""
Gera tabela Excel com referências normativas ICVM 356/01.
As duas primeiras colunas são estáticas; a terceira é preenchida via JSON.

Uso:
    python gerar_tabela_fidc.py input.json output.xlsx

Formato do JSON de entrada:
    {
        "ICVM 356/01, Art. 24, I": "Item 2º, II, 'b' da Parte Geral",
        "ICVM 356/01, Art. 24, II": "Item 2 do Anexo",
        ...
    }
    As chaves devem corresponder aos valores da coluna "Referência Normativa".
    Entradas ausentes no JSON ficam em branco.
"""

import json
import sys
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Dados estáticos
# ---------------------------------------------------------------------------
STATIC_DATA = [
    ("ICVM 356/01, Art. 24, I", "Forma de constituição, se condomínio aberto ou fechado;"),
    ("ICVM 356/01, Art. 24, II", "Taxa de administração ou critério para sua fixação;"),
    ("ICVM 356/01, Art. 24, III", "Taxa de desempenho ou de performance, quando for o caso e critério detalhado sobre a sua cobrança;"),
    ("ICVM 356/01, Art. 24, IV", "Demais taxas e despesas;"),
    ("ICVM 356/01, Art. 24, V", "Política de investimento, discriminando inclusive: (a) os critérios de elegibilidade; e (b) se for o caso, as condições de cessão dos direitos creditórios;"),
    ("ICVM 356/01, Art. 24, VI, \"a\"", "Condições para emissão, negociação, amortização e resgate de cotas, prevendo inclusive: (a) a eventual existência de mais de uma classe ou série de cotas, hipótese em que devem ser especificadas as características, os direitos e obrigações de cada uma das classes e séries, assegurando-se que as cotas subordinadas somente poderão ser resgatadas após o resgate das cotas seniores, ressalvado o disposto no art. 18-A;"),
    ("ICVM 356/01, Art. 24, VI, \"b\"", "(b) que, na amortização de cotas de fundos fechados, deverá ser assegurado que as cotas subordinadas somente poderão ser amortizadas após a amortização das cotas seniores, ressalvado o disposto no art. 18-B;"),
    ("ICVM 356/01, Art. 24, VI, \"c\"", "(c) os critérios de integralização, amortização e resgate em direitos creditórios, observado o disposto no art. 15 desta Instrução."),
    ("ICVM 356/01, Art. 24, VII", "Prazo de carência e/ou intervalo de atualização do valor da cota para fins do respectivo resgate, em se tratando de fundo aberto;"),
    ("ICVM 356/01, Art. 24, VIII", "Prazo de duração do fundo, que deverá ser determinado ou indeterminado;"),
    ("ICVM 356/01, Art. 24, IX", "Critérios de divulgação de informações aos condôminos;"),
    ("ICVM 356/01, Art. 24, X, \"a\"", "Informações sobre: (a) a natureza dos direitos creditórios a serem adquiridos e dos instrumentos jurídicos, contratos ou outros documentos representativos do crédito;"),
    ("ICVM 356/01, Art. 24, X, \"b\"", "(b) Descrição dos processos de origem dos direitos creditórios e das políticas de concessão dos correspondentes créditos;"),
    ("ICVM 356/01, Art. 24, X, \"c\"", "(c) Descrição dos mecanismos e procedimentos de cobrança dos direitos creditórios, inclusive inadimplentes, coleta e pagamento/rateio destas despesas entre os membros do condomínio, caso assim seja determinado pelo regulamento do fundo;"),
    ("ICVM 356/01, Art. 24, XI", "Quando for o caso, referência à contratação de terceiros, com a identificação e qualificação da pessoa jurídica contratada, para prestar os seguintes serviços: (a) gestão da carteira do fundo; (b) consultoria especializada; (c) custódia; e (d) agente de cobrança;"),
    ("ICVM 356/01, Art. 24, XII", "Possibilidade de nomeação de representante de condôminos, nos termos do art. 31 desta instrução;"),
    ("ICVM 356/01, Art. 24, XIII", "Metodologia de avaliação dos ativos do fundo;"),
    ("ICVM 356/01, Art. 24, XIV", "Os procedimentos a serem adotados na hipótese de rebaixamento de classificação prevista no inciso III do art. 3º da presente Instrução;"),
    ("ICVM 356/01, Art. 24, XV", "A relação mínima entre o patrimônio líquido do fundo e o valor das cotas seniores, a periodicidade para apuração e divulgação aos cotistas dessa relação e os procedimentos aplicáveis na hipótese de inobservância desse fator;"),
    ("ICVM 356/01, Art. 24, XVI", "Os eventos de liquidação antecipada do fundo, assegurando, no caso de decisão assemblear pela não liquidação do fundo, o resgate das cotas seniores, pelo valor das mesmas, aos cotistas dissidentes que o solicitarem."),
    ("ICVM 356/01, Art. 24, XVII", "Autorização para que o custodiante faça a verificação do lastro por amostragem, se for o caso, com especificação dos parâmetros relativos à diversificação de devedores, quantidade e valor médio dos créditos, a serem observados para esse fim (art. 38, §1º)."),
    ("ICVM 356/01, Art. 24, §1º, I", "A definição da política de investimento deve especificar: (I) as características gerais de atuação do fundo, entre as quais os requisitos de composição e de diversificação da carteira, os riscos de crédito e de mercado e os demais riscos envolvidos;"),
    ("ICVM 356/01, Art. 24, §1º, II", "(II) Os segmentos em que o fundo atuará;"),
    ("ICVM 356/01, Art. 24, §1º, III", "(III) A possibilidade de realização de aplicações que coloquem em risco o patrimônio do fundo;"),
    ("ICVM 356/01, Art. 24, §1º, IV", "(IV) A possibilidade de realização de operações nas quais a instituição administradora atue na condição de contraparte do fundo, desde que com a finalidade exclusiva de realizar a gestão de caixa e liquidez do fundo;"),
    ("ICVM 356/01, Art. 24, §1º, V", "(V) Que as aplicações no fundo não contam com garantia da instituição administradora ou do Fundo Garantidor de Créditos - FGC;"),
    ("ICVM 356/01, Art. 24, §1º, VI", "(VI) Observado o disposto no § 9º do art. 40-A, os limites para a realização de aplicações do fundo em ativos de emissão ou que envolvam coobrigação: (a) da instituição administradora e partes relacionadas, tal como definidas pelas regras contábeis que tratam desse assunto; e (b) dos prestadores de serviços relacionados no art. 39 e partes relacionadas, tal como definidas pelas regras contábeis que tratam desse assunto;"),
    ("ICVM 356/01, Art. 24, §1º, VII", "(VII) A validação dos direitos creditórios, observado o disposto no § 12 do art. 38, quanto: (a) aos critérios de elegibilidade, conforme dispõe o inciso I do art. 38; e (b) às condições de cessão, se for o caso, com a indicação da instituição responsável."),
]

HEADERS = [
    "Referência Normativa",
    "Descrição do Dispositivo",
    "Ex: [Item. 2º, II, \"b\" da Parte Geral; Item 2 do Anexo; Capítulo 4 do Anexo II; Suplementos]",
]

# ---------------------------------------------------------------------------
# Estilos
# ---------------------------------------------------------------------------
HEADER_FILL   = PatternFill("solid", start_color="1F4E79", end_color="1F4E79")  # azul escuro
STATIC_FILL   = PatternFill("solid", start_color="D6E4F0", end_color="D6E4F0")  # azul claro
INPUT_FILL    = PatternFill("solid", start_color="FFFDE7", end_color="FFFDE7")  # amarelo suave
ALT_ROW_FILL  = PatternFill("solid", start_color="EBF5FB", end_color="EBF5FB")  # azul muito claro (linhas pares)

HEADER_FONT   = Font(name="Arial", bold=True, color="FFFFFF", size=10)
STATIC_FONT   = Font(name="Arial", size=9)
INPUT_FONT    = Font(name="Arial", size=9)

THIN  = Side(style="thin",   color="B0BEC5")
THICK = Side(style="medium", color="1F4E79")

CELL_BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP        = Alignment(wrap_text=True, vertical="top")
HEADER_ALIGN = Alignment(wrap_text=True, vertical="center", horizontal="center")


# ---------------------------------------------------------------------------
# Função principal
# ---------------------------------------------------------------------------
def gerar_tabela(terceira_coluna: dict, output_path: str) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "ICVM 356-01"

    # --- Cabeçalho ---
    for col_idx, header in enumerate(HEADERS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font   = HEADER_FONT
        cell.fill   = HEADER_FILL
        cell.border = Border(left=THICK, right=THICK, top=THICK, bottom=THICK)
        cell.alignment = HEADER_ALIGN

    ws.row_dimensions[1].height = 40

    # --- Linhas de dados ---
    for row_idx, (ref, descricao) in enumerate(STATIC_DATA, start=2):
        fill = ALT_ROW_FILL if row_idx % 2 == 0 else STATIC_FILL

        # Col 1 – Referência Normativa
        c1 = ws.cell(row=row_idx, column=1, value=ref)
        c1.font = Font(name="Arial", size=9, bold=True)
        c1.fill = fill
        c1.border = CELL_BORDER
        c1.alignment = WRAP

        # Col 2 – Descrição do Dispositivo
        c2 = ws.cell(row=row_idx, column=2, value=descricao)
        c2.font = STATIC_FONT
        c2.fill = fill
        c2.border = CELL_BORDER
        c2.alignment = WRAP

        # Col 3 – preenchida pelo JSON (ou vazia)
        valor = terceira_coluna.get(ref, "")
        c3 = ws.cell(row=row_idx, column=3, value=valor)
        c3.font = INPUT_FONT
        c3.fill = INPUT_FILL if valor else PatternFill("solid", start_color="FFFFFF", end_color="FFFFFF")
        c3.border = CELL_BORDER
        c3.alignment = WRAP

        ws.row_dimensions[row_idx].height = 60

    # --- Larguras de coluna ---
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 70
    ws.column_dimensions["C"].width = 50

    # --- Congelar painel no cabeçalho ---
    ws.freeze_panes = "A2"

    wb.save(output_path)
    print(f"Arquivo gerado: {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Uso: python gerar_tabela_fidc.py <input.json> <output.xlsx>")
        print()
        print("Exemplo de input.json:")
        example = {
            "ICVM 356/01, Art. 24, I": "Item 2º, II, 'b' da Parte Geral",
            "ICVM 356/01, Art. 24, II": "Item 2 do Anexo",
        }
        print(json.dumps(example, ensure_ascii=False, indent=2))
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    gerar_tabela(data, sys.argv[2])
