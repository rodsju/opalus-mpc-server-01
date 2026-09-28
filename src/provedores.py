"""Provedores de IA cadastrados em catalogo.provedor_ia.

Recorte de leitura do módulo homônimo do opalus-data-connect: lá existe o CRUD
da tela Setup › AI Providers, aqui só se lê. O servidor MCP não cadastra
provedor nem troca chave -- ele consome o que a aplicação de catálogo cadastrou.

Por que a configuração vem do banco e não do .env: o vetor da pergunta precisa
sair do MESMO modelo que gerou os vetores gravados. Se divergir, o operador
`<=>` não devolve resultado ruim -- ele levanta erro de dimensão (4096 x 1536).
Uma fonte só de verdade para (url, chave, modelo) elimina essa classe de falha.

A chave fica cifrada com pgp_sym_encrypt sob IA_SENHA_MESTRA, que nunca vai para
o banco.
"""

import logging
import os
from dataclasses import dataclass

from . import catalogo

logger = logging.getLogger(__name__)


class ConfiguracaoInvalida(Exception):
    def __init__(self, motivo, detalhe=""):
        super().__init__(motivo)
        self.motivo = motivo
        self.detalhe = detalhe


def senha_mestra():
    senha = (os.getenv("IA_SENHA_MESTRA") or "").strip()
    if not senha:
        raise ConfiguracaoInvalida(
            "Senha mestra não configurada",
            "Defina IA_SENHA_MESTRA no .env deste servidor com o mesmo valor usado "
            "no opalus-data-connect. Sem ela não dá para decifrar a chave do "
            "provedor de embeddings.",
        )
    return senha


@dataclass(frozen=True)
class Provedor:
    """Um par (provedor, modelo) pronto para uso -- já com a chave decifrada."""

    slug: str
    nome: str
    tipo: str
    modelo: str
    chave: str
    url: str = ""
    capacidade: str = "llm"


SQL_ATIVOS = """
SELECT slug, nome, tipo, url, modelos, capacidade,
       pgp_sym_decrypt(chave_cifrada, %s) AS chave
  FROM catalogo.provedor_ia
 WHERE ativo
   AND capacidade = %s
 ORDER BY nome
"""


def disponiveis(capacidade="embedding"):
    """Um Provedor por par (provedor ativo, modelo), na ordem do nome."""
    try:
        linhas = catalogo.consultar(
            SQL_ATIVOS, (senha_mestra(), capacidade), f"provedores ativos de {capacidade}"
        )
    except catalogo.ConsultaFalhou as falha:
        if "Wrong key" in str(falha.causa) or "corrupt data" in str(falha.causa):
            raise ConfiguracaoInvalida(
                "Não conseguimos decifrar a chave do provedor",
                "A senha mestra em IA_SENHA_MESTRA não é a mesma que cifrou os "
                "provedores. Copie o valor usado no opalus-data-connect.",
            ) from falha
        raise

    return [
        Provedor(
            slug=linha["slug"],
            nome=linha["nome"],
            tipo=linha["tipo"],
            modelo=modelo,
            chave=linha["chave"],
            url=linha["url"],
            capacidade=linha["capacidade"],
        )
        for linha in linhas
        for modelo in linha["modelos"]
    ]


def escolher(valor=None, capacidade="embedding"):
    """Provedor do par "slug::modelo" pedido, ou o primeiro da capacidade."""
    pares = disponiveis(capacidade)
    if not pares:
        raise ConfiguracaoInvalida(
            f"Nenhum provedor de {capacidade} configurado",
            f"Cadastre um em Setup › AI Providers com capacidade '{capacidade}'.",
        )
    if not valor:
        return pares[0]

    slug, _, modelo = valor.partition("::")
    for par in pares:
        if par.slug == slug and (not modelo or par.modelo == modelo):
            return par
    raise ConfiguracaoInvalida(
        f"O provedor '{valor}' não está ativo ou não tem capacidade '{capacidade}'"
    )
