from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

import psycopg
from dotenv import load_dotenv
from src.utils import slugify as _slugify

_slug = lambda v: _slugify(v, fallback=None)

load_dotenv()

log = logging.getLogger("aidocs")

RAGDB_URL = os.getenv("DATABASE_URL")

# ── DDL ───────────────────────────────────────────────────────────────────────

DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

-- migrações: renomeia tabelas plurais para singular (seguro em bases já existentes)
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='emissions')
     AND NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='emission') THEN
    ALTER TABLE emissions RENAME TO emission;
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='patrimonies')
     AND NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='patrimony') THEN
    ALTER TABLE patrimonies RENAME TO patrimony;
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='emission_files')
     AND NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='emission_file') THEN
    ALTER TABLE emission_files RENAME TO emission_file;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS emission (
    id                          INTEGER PRIMARY KEY,
    number                      TEXT,
    date                        DATE,
    emission_name               TEXT,
    emission_slug               TEXT,
    emission_code_name          TEXT,
    concentration               TEXT,
    approved                    TEXT,
    origination                 TEXT,
    type_id                     INTEGER,
    segment_id                  INTEGER,
    anbima_classification_id    INTEGER,
    company_issuing_id          INTEGER,
    custodian_id                INTEGER,
    trustee_id                  INTEGER,
    originator_id               INTEGER,
    debtor_id                   INTEGER,
    responsible_principal_id    INTEGER,
    condominium_type                    TEXT,
    revolving                           BOOLEAN,
    total_emitted_volume                NUMERIC,
    mandate_date                        DATE,
    is_showing_ri_portal                BOOLEAN,
    has_fiduciary_regime                BOOLEAN,
    cvm_accounting_obligations          TEXT,
    production_chain                    TEXT,
    segment_type                        TEXT,
    structure_text                      TEXT,
    other_relevant_characteristics      TEXT,
    statements_inactivation_flag        BOOLEAN,
    statements_inactivation_date        DATE,
    accounting_publication              TEXT,
    assign_or_id                        INTEGER,
    company_id                          INTEGER,
    customer_id                         INTEGER,
    participant_customer_id             INTEGER,
    settlement_account_id               INTEGER,
    who_brought_id                      INTEGER,
    number_patrimonies                  INTEGER,
    is_manual_name                      BOOLEAN,
    is_showing_vert_data                BOOLEAN,
    specifying_details_production_chain TEXT,
    specifying_details_segment_type     TEXT,
    created_at                          TIMESTAMPTZ,
    updated_at                          TIMESTAMPTZ,
    deleted_at                          TIMESTAMPTZ,
    downloaded_main_docs                BOOLEAN DEFAULT FALSE,
    emission_type                       TEXT
);

ALTER TABLE emission ADD COLUMN IF NOT EXISTS downloaded_main_docs BOOLEAN DEFAULT FALSE;
ALTER TABLE emission ADD COLUMN IF NOT EXISTS emission_type TEXT;

-- patrimony: CREATE mínimo + ADD COLUMN IF NOT EXISTS para idempotência
CREATE TABLE IF NOT EXISTS patrimony (id INTEGER PRIMARY KEY);
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS number INTEGER;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS emission_id INTEGER REFERENCES emission(id) ON DELETE CASCADE;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS status_mark TEXT;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS accounting_closing TEXT;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS accounting_obligations TEXT;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS credit_purchase_period_start DATE;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS credit_purchase_period_end DATE;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS payment_period_start DATE;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS payment_period_end DATE;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS closing_period_start DATE;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS closing_period_end DATE;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS balance_date TIMESTAMPTZ;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS total_all_accounts NUMERIC;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS total_available_cash NUMERIC;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS total_current_accounts NUMERIC;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS total_investments NUMERIC;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS total_restricted_cash NUMERIC;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS statement_consumption BOOLEAN;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS canceled_date DATE;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS cutoff_date DATE;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS cutoff_date_contract DATE;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS bank_statement_auto_creation_start_date DATE;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
ALTER TABLE patrimony ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- emission_file: CREATE mínimo + ADD COLUMN IF NOT EXISTS para idempotência
CREATE TABLE IF NOT EXISTS emission_file (id INTEGER PRIMARY KEY);
ALTER TABLE emission_file ADD COLUMN IF NOT EXISTS emission_id INTEGER REFERENCES emission(id) ON DELETE CASCADE;
ALTER TABLE emission_file ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE emission_file ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE emission_file ADD COLUMN IF NOT EXISTS file_url TEXT;
ALTER TABLE emission_file ADD COLUMN IF NOT EXISTS file_name TEXT;
ALTER TABLE emission_file ADD COLUMN IF NOT EXISTS file_content_type TEXT;
ALTER TABLE emission_file ADD COLUMN IF NOT EXISTS public BOOLEAN;
ALTER TABLE emission_file ADD COLUMN IF NOT EXISTS reference_date DATE;
ALTER TABLE emission_file ADD COLUMN IF NOT EXISTS assemblies_date DATE;
ALTER TABLE emission_file ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE emission_file ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- emission_ambima: CREATE mínimo + ADD COLUMN IF NOT EXISTS para idempotência
CREATE TABLE IF NOT EXISTS emission_ambima (id SERIAL PRIMARY KEY);
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS emission_id INTEGER UNIQUE REFERENCES emission(id) ON DELETE CASCADE;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS estrategia TEXT;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS estrutura TEXT;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS prazo_medio_ponderado TEXT;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS performance_do_credito TEXT;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS setor_do_cedente TEXT;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS tipo_de_lastro TEXT;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS natureza_do_direito_creditorio TEXT;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS garantias TEXT;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS coobrigacao_do_cedente TEXT;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS originacao TEXT;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS agencia_classificadora_de_risco TEXT;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS indice_de_subordinacao TEXT;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS observacoes_sobre_a_classificacao TEXT;
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE emission_ambima ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- emission_summary: uma linha por (h1, h2); quando H1 sem H2 ou com intro, h2 repete h1
CREATE TABLE IF NOT EXISTS emission_summary (id SERIAL PRIMARY KEY);
ALTER TABLE emission_summary ADD COLUMN IF NOT EXISTS emission_id INTEGER REFERENCES emission(id) ON DELETE CASCADE;
ALTER TABLE emission_summary ADD COLUMN IF NOT EXISTS h1 TEXT;
ALTER TABLE emission_summary ADD COLUMN IF NOT EXISTS h2 TEXT;
ALTER TABLE emission_summary ADD COLUMN IF NOT EXISTS h1_slug TEXT;
ALTER TABLE emission_summary ADD COLUMN IF NOT EXISTS h2_slug TEXT;
ALTER TABLE emission_summary ADD COLUMN IF NOT EXISTS content TEXT;
ALTER TABLE emission_summary ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE emission_summary ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
CREATE UNIQUE INDEX IF NOT EXISTS emission_summary_unique ON emission_summary (emission_id, h1, h2);

-- emission_chunks: chunks vetorizados do canonical.md
CREATE TABLE IF NOT EXISTS emission_chunks (id UUID PRIMARY KEY);
ALTER TABLE emission_chunks ADD COLUMN IF NOT EXISTS emission_id   INTEGER REFERENCES emission(id) ON DELETE CASCADE;
ALTER TABLE emission_chunks ADD COLUMN IF NOT EXISTS source_file   TEXT;
ALTER TABLE emission_chunks ADD COLUMN IF NOT EXISTS section_path  TEXT;
ALTER TABLE emission_chunks ADD COLUMN IF NOT EXISTS chunk_index   INTEGER;
ALTER TABLE emission_chunks ADD COLUMN IF NOT EXISTS chunk_text    TEXT;
ALTER TABLE emission_chunks ADD COLUMN IF NOT EXISTS char_count    INTEGER;
ALTER TABLE emission_chunks ADD COLUMN IF NOT EXISTS chunk_hash    TEXT;
ALTER TABLE emission_chunks ADD COLUMN IF NOT EXISTS embedding_model TEXT;
ALTER TABLE emission_chunks ADD COLUMN IF NOT EXISTS embedding_dim INTEGER;
ALTER TABLE emission_chunks ADD COLUMN IF NOT EXISTS embedding     VECTOR(4096);
ALTER TABLE emission_chunks ADD COLUMN IF NOT EXISTS created_at    TIMESTAMPTZ DEFAULT NOW();
CREATE UNIQUE INDEX IF NOT EXISTS emission_chunks_unique ON emission_chunks (emission_id, chunk_hash);
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _date(v: Any) -> Any:
    return v or None


def _ts(v: Any) -> Any:
    return v or None


import unicodedata as _ud

_TYPE_PATTERNS: list[tuple[str, list[str]]] = [
    ("fidc", [
        r"\bfidc\b",
        r"fundo\s+de\s+investimento\s+em\s+direitos\s+creditorios",
    ]),
    ("fiagro", [
        r"\bfiagro\b",
        r"fundo\s+de\s+investimento\s+nas\s+cadeias\s+produtivas\s+agroindustriais",
        r"fundo\s+de\s+investimento\s+nas\s+cadeias\s+produtivas\s+do\s+agronegocio",
        r"fundo\s+de\s+investimento\s+nas\s+cadeias\s+produtivas",
    ]),
    ("fic", [
        r"\bfic\b",
        r"fundo\s+de\s+investimento\s+em\s+cotas\s+de\s+fundos",
    ]),
    ("cra", [
        r"\bcra\b",
        r"certificado\s+de\s+receb[ia]vel\s+agr[ia]cola",
    ]),
    ("cri", [
        r"\bcri\b",
        r"certificado\s+de\s+receb[ia]vel\s+imobiliar[io]",
    ]),
    ("cr", [
        r"(?<![a-z\d])cr(?![a-z\d])",
        r"certificados\s+de\s+receb[ia]ve[il]s",
    ]),
    ("debenture", [
        r"\bfdeb\b",
        r"\bdeb\b",
        r"\bdebent",
    ]),
    ("nc", [
        r"\bnc\b",
        r"notas\s+comerciais",
    ]),
    ("fii", [
        r"\bfii\b",
        r"fundo\s+de\s+investimento\s+imobiliario",
    ]),
]


def infer_emission_type(name: str | None) -> str | None:
    if not name:
        return None
    normalized = _ud.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    for emission_type, patterns in _TYPE_PATTERNS:
        for pat in patterns:
            if re.search(pat, normalized):
                return emission_type
    return None


def backfill_emission_types(db_url: str = RAGDB_URL) -> dict:
    with psycopg.connect(db_url) as conn:
        rows = conn.execute("SELECT id, emission_name FROM emission").fetchall()
        updated = 0
        for eid, name in rows:
            etype = infer_emission_type(name)
            if etype is not None:
                cur = conn.execute(
                    "UPDATE emission SET emission_type = %s WHERE id = %s AND (emission_type IS DISTINCT FROM %s)",
                    (etype, eid, etype),
                )
                if cur.rowcount > 0:
                    updated += 1
        conn.commit()
    result = {"total": len(rows), "updated": updated}
    log.info("[emission_type] backfill concluído: %s", result)
    return result


def _emission_row(r: dict) -> dict:
    p = "Core Emission - Emission → "
    return {
        "id":                           r[f"{p}ID"],
        "number":                       r.get(f"{p}Number"),
        "date":                         _date(r.get(f"{p}Date")),
        "emission_name":                r.get(f"{p}Emission Name"),
        "emission_slug":                _slug(r.get(f"{p}Emission Name")),
        "emission_code_name":           r.get(f"{p}Emission Code Name"),
        "concentration":                r.get(f"{p}Concentration"),
        "approved":                     r.get(f"{p}Approved"),
        "origination":                  r.get(f"{p}Origination"),
        "type_id":                      r.get(f"{p}Type ID"),
        "segment_id":                   r.get(f"{p}Segment ID"),
        "anbima_classification_id":     r.get(f"{p}Anbima Classification ID"),
        "company_issuing_id":           r.get(f"{p}Company Issuing ID"),
        "custodian_id":                 r.get(f"{p}Custodian ID"),
        "trustee_id":                   r.get(f"{p}Trustee ID"),
        "originator_id":                r.get(f"{p}Originator ID"),
        "debtor_id":                    r.get(f"{p}Debtor ID"),
        "responsible_principal_id":     r.get(f"{p}Responsible Principal ID"),
        "condominium_type":             r.get(f"{p}Condominium Type"),
        "revolving":                    r.get(f"{p}Revolving"),
        "total_emitted_volume":         r.get(f"{p}Total Emitted Volume"),
        "mandate_date":                 _date(r.get(f"{p}Mandate Date")),
        "is_showing_ri_portal":         r.get(f"{p}Is Showing Ri Portal"),
        "has_fiduciary_regime":         r.get(f"{p}Has Fiduciary Regime Institution"),
        "cvm_accounting_obligations":   r.get(f"{p}Cvm Accounting Obligations"),
        "production_chain":             r.get(f"{p}Production Chain"),
        "segment_type":                 r.get(f"{p}Segment Type"),
        "structure_text":               r.get(f"{p}Structure Text"),
        "other_relevant_characteristics": r.get(f"{p}Other Relevant Characteristics Emission"),
        "statements_inactivation_flag":             r.get(f"{p}Statements Inactivation Flag"),
        "statements_inactivation_date":             _date(r.get(f"{p}Statements Inactivation Date")),
        "accounting_publication":                   r.get(f"{p}Accounting Publication"),
        "assign_or_id":                             r.get(f"{p}Assign Or ID"),
        "company_id":                               r.get(f"{p}Company ID"),
        "customer_id":                              r.get(f"{p}Customer ID"),
        "participant_customer_id":                  r.get(f"{p}Participant Customer ID"),
        "settlement_account_id":                    r.get(f"{p}Settlement Account ID"),
        "who_brought_id":                           r.get(f"{p}Who Brought ID"),
        "number_patrimonies":                       r.get(f"{p}Number Patrimonies"),
        "is_manual_name":                           r.get(f"{p}Is Manual Name"),
        "is_showing_vert_data":                     r.get(f"{p}Is Showing Vert Data"),
        "specifying_details_production_chain":      r.get(f"{p}Specifying Details Production Chain"),
        "specifying_details_segment_type":          r.get(f"{p}Specifying Details Segment Type"),
        "created_at":                               _ts(r.get(f"{p}Created At")),
        "updated_at":                               _ts(r.get(f"{p}Updated At")),
        "deleted_at":                               _ts(r.get(f"{p}Deleted At")),
        "emission_type":                            infer_emission_type(r.get(f"{p}Emission Name")),
    }


def _patrimony_row(r: dict) -> dict:
    return {
        "id":                           r["ID"],
        "number":                       r.get("Number"),
        "emission_id":                  r.get("Emission ID"),
        "status":                       r.get("Status"),
        "status_mark":                  r.get("Status Mark"),
        "accounting_closing":           r.get("Accounting Closing"),
        "accounting_obligations":       r.get("Accounting Obligations"),
        "credit_purchase_period_start": _date(r.get("Credit Purchase Period Start Date")),
        "credit_purchase_period_end":   _date(r.get("Credit Purchase Period End Date")),
        "payment_period_start":         _date(r.get("Payment Period Start Date")),
        "payment_period_end":           _date(r.get("Payment Period End Date")),
        "closing_period_start":         _date(r.get("Closing Period Start Date")),
        "closing_period_end":           _date(r.get("Closing Period End Date")),
        "balance_date":                 _ts(r.get("Balance Date")),
        "total_all_accounts":           r.get("Total All Accounts"),
        "total_available_cash":         r.get("Total Available Cash"),
        "total_current_accounts":       r.get("Total Current Accounts"),
        "total_investments":            r.get("Total Investments"),
        "total_restricted_cash":        r.get("Total Restricted Cash"),
        "statement_consumption":        r.get("Statement Consumption"),
        "canceled_date":                            _date(r.get("Canceled Date")),
        "cutoff_date":                              _date(r.get("Cutoff Date")),
        "cutoff_date_contract":                     _date(r.get("Cutoff Date Contract")),
        "bank_statement_auto_creation_start_date":  _date(r.get("Bank Statement Auto Creation Start Date")),
        "created_at":                               _ts(r.get("Created At")),
        "updated_at":                               _ts(r.get("Updated At")),
        "deleted_at":                               _ts(r.get("Deleted At")),
    }


def _file_row(r: dict) -> dict:
    p = "Files Emission File → "
    return {
        "id":               r[f"{p}ID"],
        "emission_id":      r.get(f"{p}Emission ID"),
        "category":         r.get(f"{p}Category"),
        "name":             r.get(f"{p}Name"),
        "file_url":         r.get(f"{p}File URL"),
        "file_name":        r.get(f"{p}File Name"),
        "file_content_type": r.get(f"{p}File Content Type"),
        "public":           r.get(f"{p}Public"),
        "reference_date":   _date(r.get(f"{p}Reference Date")),
        "assemblies_date":  _date(r.get(f"{p}Assemblies Date")),
        "created_at":       _ts(r.get(f"{p}Created At")),
        "deleted_at":       _ts(r.get(f"{p}Deleted At")),
    }


# ── Core functions ────────────────────────────────────────────────────────────

def create_tables(db_url: str = RAGDB_URL) -> None:
    with psycopg.connect(db_url) as conn:
        conn.execute(DDL)
        conn.commit()
    log.info("[emissions] Tabelas criadas/verificadas")


def import_from_json(json_path: str | Path, db_url: str = RAGDB_URL) -> dict:
    path = Path(json_path)
    log.info("[emissions] Lendo %s", path.name)

    with open(path, encoding="utf-8") as f:
        records = json.load(f)

    emissions: dict[int, dict] = {}
    patrimonies: dict[int, dict] = {}
    files: dict[int, dict] = {}

    for r in records:
        e = _emission_row(r)
        emissions[e["id"]] = e

        pat = _patrimony_row(r)
        patrimonies[pat["id"]] = pat

        file = _file_row(r)
        if file["id"] is not None:
            files[file["id"]] = file

    log.info("[emissions] %d emissões | %d patrimônios | %d arquivos",
             len(emissions), len(patrimonies), len(files))

    with psycopg.connect(db_url) as conn:
        for row in emissions.values():
            cols = ", ".join(row.keys())
            vals = ", ".join(f"%({k})s" for k in row)
            updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in row if k != "id")
            conn.execute(
                f"INSERT INTO emission ({cols}) VALUES ({vals}) "
                f"ON CONFLICT (id) DO UPDATE SET {updates}",
                row,
            )

        for row in patrimonies.values():
            cols = ", ".join(row.keys())
            vals = ", ".join(f"%({k})s" for k in row)
            updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in row if k != "id")
            conn.execute(
                f"INSERT INTO patrimony ({cols}) VALUES ({vals}) "
                f"ON CONFLICT (id) DO UPDATE SET {updates}",
                row,
            )

        for row in files.values():
            cols = ", ".join(row.keys())
            vals = ", ".join(f"%({k})s" for k in row)
            updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in row if k != "id")
            conn.execute(
                f"INSERT INTO emission_file ({cols}) VALUES ({vals}) "
                f"ON CONFLICT (id) DO UPDATE SET {updates}",
                row,
            )

        conn.commit()

    summary = {
        "emission": len(emissions),
        "patrimony": len(patrimonies),
        "emission_file": len(files),
    }
    log.info("[emissions] Import concluído: %s", summary)
    return summary


S3_BASE = "https://vertc-ops-prd-sp-media.s3.amazonaws.com/"


def download_files(
    dest_dir: str | Path = "/app/files/source_docs",
    db_url: str = RAGDB_URL,
    emission_id: int | None = None,
    category: str = "REGULATIONS",
    last: bool = True,
) -> list[Path]:
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    filter_sql = "AND e.id = %(emission_id)s" if emission_id is not None else ""
    params: dict = {"category": category}
    if emission_id is not None:
        params["emission_id"] = emission_id

    if last:
        select_clause = "SELECT DISTINCT ON (e.emission_slug) ef.file_url, ef.file_name, ef.name, e.emission_slug, e.id"
        order_clause  = "ORDER BY e.emission_slug, ef.id DESC"
    else:
        select_clause = "SELECT ef.file_url, ef.file_name, ef.name, e.emission_slug, e.id"
        order_clause  = "ORDER BY e.emission_slug, ef.id DESC"

    with psycopg.connect(db_url) as conn:
        rows = conn.execute(
            f"""
            {select_clause}
            FROM emission_file ef
            JOIN emission e ON e.id = ef.emission_id
            WHERE ef.category = %(category)s
              AND ef.deleted_at IS NULL
              AND ef.file_url IS NOT NULL
              {filter_sql}
            {order_clause}
            """,
            params,
        ).fetchall()

    downloaded: list[Path] = []
    flagged_ids: list[int] = []

    with httpx.Client(timeout=60) as client:
        for file_url, file_name, file_doc_name, emission_slug, emission_id in rows:
            url = urljoin(S3_BASE, file_url)
            ext = Path(file_name or file_url).suffix or ".pdf"
            slug = emission_slug or _slug(file_url) or str(hash(file_url))
            doc_slug = (_slug(file_doc_name) + "_") if not last and file_doc_name else ""
            dest_path = dest / f"{emission_id}_{slug}_{doc_slug}{_slug(category)}{ext}"

            if dest_path.exists():
                log.info("[regulations] já existe, pulando: %s", dest_path.name)
                downloaded.append(dest_path)
                flagged_ids.append(emission_id)
                continue

            log.info("[regulations] baixando %s → %s", url, dest_path.name)
            try:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with open(dest_path, "wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            fh.write(chunk)
                downloaded.append(dest_path)
                flagged_ids.append(emission_id)
            except httpx.HTTPError as exc:
                log.warning("[regulations] falha ao baixar %s: %s", url, exc)

    if flagged_ids:
        with psycopg.connect(db_url) as conn:
            conn.execute(
                "UPDATE emission SET downloaded_main_docs = TRUE WHERE id = ANY(%s)",
                (flagged_ids,),
            )
            conn.commit()
        log.info("[regulations] downloaded_main_docs atualizado para %d emissões", len(flagged_ids))

    log.info("[regulations] %d arquivos em %s", len(downloaded), dest)
    return downloaded


def sync_downloaded_flag(
    output_root: str | Path = "/app/files/output",
    db_url: str = RAGDB_URL,
) -> dict:
    root = Path(output_root)
    emission_ids: set[int] = set()

    for entry in root.iterdir():
        if entry.is_dir():
            prefix = entry.name.split("_")[0]
            if prefix.isdigit():
                emission_ids.add(int(prefix))

    if not emission_ids:
        log.info("[downloaded] Nenhum diretório com prefixo numérico em %s", root)
        return {"updated": 0}

    with psycopg.connect(db_url) as conn:
        cur = conn.execute(
            "UPDATE emission SET downloaded_main_docs = TRUE WHERE id = ANY(%s) AND downloaded_main_docs IS DISTINCT FROM TRUE",
            (list(emission_ids),),
        )
        conn.commit()

    result = {"scanned": len(emission_ids), "updated": cur.rowcount}
    log.info("[downloaded] %s", result)
    return result


def rename_output_dirs(
    output_root: str | Path = "/app/files/output",
) -> dict:
    """Remove o sufixo de tipo das pastas de output (ex: _regulations → nada).

    Se a pasta destino já existir, apaga a mais antiga e mantém a mais nova.
    """
    import shutil

    root = Path(output_root)
    renamed = deleted = skipped = 0

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue

        parts = entry.name.split("_")
        if len(parts) < 3 or not parts[0].isdigit():
            continue

        target_name = "_".join(parts[:-1])
        target = root / target_name

        if target == entry:
            continue

        if target.exists():
            src_mtime = entry.stat().st_mtime
            tgt_mtime = target.stat().st_mtime

            if src_mtime >= tgt_mtime:
                shutil.rmtree(target)
                log.info("[rename-output] removida pasta mais antiga: %s", target.name)
                deleted += 1
                entry.rename(target)
                log.info("[rename-output] renomeada: %s → %s", entry.name, target_name)
                renamed += 1
            else:
                shutil.rmtree(entry)
                log.info("[rename-output] removida pasta mais antiga: %s", entry.name)
                deleted += 1
        else:
            entry.rename(target)
            log.info("[rename-output] renomeada: %s → %s", entry.name, target_name)
            renamed += 1

    result = {"renamed": renamed, "deleted": deleted, "skipped": skipped}
    log.info("[rename-output] concluído: %s", result)
    return result


_AMBIMA_FIELD_MAP = {
    "Estrategia":                       "estrategia",
    "Estrutura":                        "estrutura",
    "Prazo_Medio_Ponderado":            "prazo_medio_ponderado",
    "Performance_do_Credito":           "performance_do_credito",
    "Setor_do_Cedente":                 "setor_do_cedente",
    "Tipo_de_Lastro":                   "tipo_de_lastro",
    "Natureza_do_Direito_Creditorio":   "natureza_do_direito_creditorio",
    "Garantias":                        "garantias",
    "Coobrigacao_do_Cedente":           "coobrigacao_do_cedente",
    "Originacao":                       "originacao",
    "Agencia_Classificadora_de_Risco":  "agencia_classificadora_de_risco",
    "Indice_de_Subordinacao":           "indice_de_subordinacao",
    "Observacoes_Sobre_a_Classificacao": "observacoes_sobre_a_classificacao",
}


def import_ambima_classifications(
    output_root: str | Path = "/app/files/output",
    db_url: str = RAGDB_URL,
) -> dict:
    root = Path(output_root)
    files = sorted(root.glob("**/*_ambima_class.json"))

    if not files:
        log.info("[ambima] Nenhum arquivo *_ambima_class.json encontrado em %s", root)
        return {"processed": 0, "skipped": 0, "errors": 0}

    processed = skipped = errors = 0

    with psycopg.connect(db_url) as conn:
        valid_ids: set[int] = {
            row[0] for row in conn.execute("SELECT id FROM emission").fetchall()
        }

        for path in files:
            prefix = path.stem.split("_")[0]
            if not prefix.isdigit():
                log.warning("[ambima] ID inválido em %s, pulando", path.name)
                skipped += 1
                continue

            emission_id = int(prefix)

            if emission_id not in valid_ids:
                log.warning("[ambima] emission_id=%d não existe na tabela emission (%s), pulando", emission_id, path.name)
                skipped += 1
                continue

            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.error("[ambima] Erro ao ler %s: %s", path.name, exc)
                errors += 1
                continue

            row = {"emission_id": emission_id}
            for json_key, col in _AMBIMA_FIELD_MAP.items():
                row[col] = data.get(json_key)

            cols = ", ".join(row.keys())
            vals = ", ".join(f"%({k})s" for k in row)
            updates = ", ".join(
                f"{col} = EXCLUDED.{col}" for col in _AMBIMA_FIELD_MAP.values()
            ) + ", updated_at = NOW()"

            conn.execute(
                f"INSERT INTO emission_ambima ({cols}) VALUES ({vals}) "
                f"ON CONFLICT (emission_id) DO UPDATE SET {updates}",
                row,
            )
            log.info("[ambima] upsert emission_id=%d (%s)", emission_id, path.name)
            processed += 1

        conn.commit()

    summary = {"processed": processed, "skipped": skipped, "errors": errors}
    log.info("[ambima] Concluído: %s", summary)
    return summary


_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"  # Misc symbols & pictographs
    "\U0001F600-\U0001F64F"  # Emoticons
    "\U0001F680-\U0001F6FF"  # Transport & map
    "\U0001F900-\U0001F9FF"  # Supplemental symbols
    "\U00002600-\U000027BF"  # Misc symbols (⚠ etc.)
    "\U0001F1E0-\U0001F1FF"  # Regional indicator letters
    "︀-️"          # Variation selectors (U+FE0F é o mais comum)
    "​-‏"          # Zero-width space, ZWNJ, ZWJ, LRM, RLM
    "⁠-⁤"          # Word joiner / invisible operators
    "﻿"                 # BOM / zero-width no-break space
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def _parse_summary_md(text: str) -> list[dict]:
    """Converte um _summary.md em lista de {h1, h2, content}.

    Regras:
    - H1 sem H2: h2 = h1
    - H1 com texto introdutório antes do primeiro H2: emite linha com h2 = h1
    - H1 com H2: emite uma linha por H2
    """
    rows: list[dict] = []

    # divide o texto nos blocos H1 (mantém o marcador '#' junto)
    h1_blocks = re.split(r'\n(?=# (?!#))', '\n' + text.strip())

    for block in h1_blocks:
        block = block.strip()
        if not block.startswith('# '):
            continue

        first_line, _, body = block.partition('\n')
        h1_title = _strip_emoji(first_line.lstrip('#').strip())
        body = re.sub(r'\n?---\n?', '\n', body).strip()

        # divide nos blocos H2
        h2_blocks = re.split(r'\n(?=## )', '\n' + body)
        intro = h2_blocks[0].strip()
        h2_sections = [b.strip() for b in h2_blocks[1:] if b.strip()]

        if not h2_sections:
            # H1 sem H2: tudo vai numa linha com h2 = h1
            if intro:
                rows.append({'h1': h1_title, 'h2': h1_title, 'content': intro})
        else:
            # texto introdutório antes do primeiro H2 → h2 = h1
            if intro:
                rows.append({'h1': h1_title, 'h2': h1_title, 'content': intro})
            for h2_block in h2_sections:
                h2_first, _, h2_body = h2_block.partition('\n')
                h2_title = _strip_emoji(h2_first.lstrip('#').strip())
                h2_content = h2_body.strip()
                if h2_content:
                    rows.append({'h1': h1_title, 'h2': h2_title, 'content': h2_content})

    return rows


def import_summary_md(
    output_root: str | Path = "/app/files/output",
    db_url: str = RAGDB_URL,
) -> dict:
    root = Path(output_root)
    files = sorted(root.glob("**/*_summary.md"))

    if not files:
        log.info("[summary] Nenhum arquivo *_summary.md encontrado em %s", root)
        return {"processed": 0, "skipped": 0, "errors": 0}

    processed = skipped = errors = 0

    with psycopg.connect(db_url) as conn:
        valid_ids: set[int] = {
            row[0] for row in conn.execute("SELECT id FROM emission").fetchall()
        }

        for path in files:
            prefix = path.stem.split("_")[0]
            if not prefix.isdigit():
                log.warning("[summary] ID inválido em %s, pulando", path.name)
                skipped += 1
                continue

            emission_id = int(prefix)

            if emission_id not in valid_ids:
                log.warning("[summary] emission_id=%d não existe na tabela emission (%s), pulando", emission_id, path.name)
                skipped += 1
                continue

            try:
                text = path.read_text(encoding="utf-8")
                rows = _parse_summary_md(text)
            except Exception as exc:
                log.error("[summary] Erro ao ler %s: %s", path.name, exc)
                errors += 1
                continue

            if not rows:
                log.warning("[summary] Nenhuma seção encontrada em %s", path.name)
                skipped += 1
                continue

            for row in rows:
                row["h1"]      = row["h1"].strip()
                row["h2"]      = row["h2"].strip()
                row["h1_slug"] = _slug(row["h1"])
                row["h2_slug"] = _slug(row["h2"])
                row["emission_id"] = emission_id
                conn.execute(
                    """
                    INSERT INTO emission_summary (emission_id, h1, h2, h1_slug, h2_slug, content)
                    VALUES (%(emission_id)s, %(h1)s, %(h2)s, %(h1_slug)s, %(h2_slug)s, %(content)s)
                    ON CONFLICT (emission_id, h1, h2)
                    DO UPDATE SET
                        h1_slug = EXCLUDED.h1_slug,
                        h2_slug = EXCLUDED.h2_slug,
                        content = EXCLUDED.content,
                        updated_at = NOW()
                    """,
                    row,
                )

            log.info("[summary] upsert emission_id=%d — %d seções (%s)", emission_id, len(rows), path.name)
            processed += 1

        conn.commit()

    result = {"processed": processed, "skipped": skipped, "errors": errors}
    log.info("[summary] Concluído: %s", result)
    return result


def trim_summary(db_url: str = RAGDB_URL) -> dict:
    with psycopg.connect(db_url) as conn:
        rows = conn.execute("SELECT id, h1, h2 FROM emission_summary").fetchall()

        updated = deleted = 0
        seen: dict[tuple, int] = {}  # (emission_id, clean_h1, clean_h2) → id

        # busca emission_id junto para deduplicar
        rows_full = conn.execute("SELECT id, emission_id, h1, h2 FROM emission_summary ORDER BY id").fetchall()

        for row_id, emission_id, h1, h2 in rows_full:
            clean_h1 = _strip_emoji(h1).strip()
            clean_h2 = _strip_emoji(h2).strip()
            key = (emission_id, clean_h1, clean_h2)

            if key in seen:
                conn.execute("DELETE FROM emission_summary WHERE id = %s", (row_id,))
                deleted += 1
                continue

            seen[key] = row_id

            if clean_h1 != h1 or clean_h2 != h2:
                conn.execute(
                    """
                    UPDATE emission_summary
                    SET h1 = %s, h2 = %s,
                        h1_slug = %s, h2_slug = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (clean_h1, clean_h2, _slug(clean_h1), _slug(clean_h2), row_id),
                )
                updated += 1

        conn.commit()

    result = {"deleted_duplicates": deleted, "updated": updated}
    log.info("[trim-summary] %s", result)
    return result


def import_canonical_chunks(
    output_root: str | Path = "/app/files/output",
    db_url: str = RAGDB_URL,
) -> dict:
    from src.chunker import build_embed_fn, chunk_and_embed

    root = Path(output_root)
    files = sorted(root.glob("**/*_canonical.md"))

    if not files:
        log.info("[chunks] Nenhum arquivo *_canonical.md encontrado em %s", root)
        return {"processed": 0, "skipped": 0, "errors": 0}

    processed = skipped = errors = 0
    embed_fn = build_embed_fn()

    with psycopg.connect(db_url) as conn:
        valid_ids: set[int] = {
            row[0] for row in conn.execute("SELECT id FROM emission").fetchall()
        }

        for path in files:
            prefix = path.stem.split("_")[0]
            if not prefix.isdigit():
                log.warning("[chunks] ID inválido em %s, pulando", path.name)
                skipped += 1
                continue

            emission_id = int(prefix)

            if emission_id not in valid_ids:
                log.warning("[chunks] emission_id=%d não existe (%s), pulando", emission_id, path.name)
                skipped += 1
                continue

            try:
                rows = chunk_and_embed(path, embed_fn)
            except Exception as exc:
                log.error("[chunks] Erro ao processar %s: %s", path.name, exc)
                errors += 1
                continue

            if not rows:
                skipped += 1
                continue

            for row in rows:
                row["emission_id"] = emission_id
                conn.execute(
                    """
                    INSERT INTO emission_chunks
                        (id, emission_id, source_file, section_path, chunk_index,
                         chunk_text, char_count, chunk_hash, embedding_model, embedding_dim, embedding)
                    VALUES
                        (%(id)s, %(emission_id)s, %(source_file)s, %(section_path)s, %(chunk_index)s,
                         %(chunk_text)s, %(char_count)s, %(chunk_hash)s, %(embedding_model)s, %(embedding_dim)s,
                         %(embedding)s::vector)
                    ON CONFLICT (emission_id, chunk_hash)
                    DO UPDATE SET
                        section_path   = EXCLUDED.section_path,
                        chunk_index    = EXCLUDED.chunk_index,
                        chunk_text     = EXCLUDED.chunk_text,
                        char_count     = EXCLUDED.char_count,
                        embedding_model = EXCLUDED.embedding_model,
                        embedding_dim  = EXCLUDED.embedding_dim,
                        embedding      = EXCLUDED.embedding
                    """,
                    row,
                )

            conn.commit()
            log.info("[chunks] upsert emission_id=%d — %d chunks (%s)", emission_id, len(rows), path.name)
            processed += 1

    result = {"processed": processed, "skipped": skipped, "errors": errors}
    log.info("[chunks] Concluído: %s", result)
    return result


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "import"

    if cmd == "download":
        dest = sys.argv[2] if len(sys.argv) > 2 else "/app/files/source_docs"
        eid = int(sys.argv[3]) if len(sys.argv) > 3 else None
        cat = sys.argv[4] if len(sys.argv) > 4 else "REGULATIONS"
        last = "--all" not in sys.argv
        files = download_files(dest, emission_id=eid, category=cat, last=last)
        print(f"{len(files)} arquivos baixados em {dest}")
    elif cmd == "ambima":
        output_root = sys.argv[2] if len(sys.argv) > 2 else "/app/files/output"
        result = import_ambima_classifications(output_root)
        print(result)
    elif cmd == "summary":
        output_root = sys.argv[2] if len(sys.argv) > 2 else "/app/files/output"
        result = import_summary_md(output_root)
        print(result)
    elif cmd == "chunks":
        output_root = sys.argv[2] if len(sys.argv) > 2 else "/app/files/output"
        result = import_canonical_chunks(output_root)
        print(result)
    elif cmd == "backfill-types":
        result = backfill_emission_types()
        print(result)
    elif cmd == "trim-summary":
        result = trim_summary()
        print(result)
    elif cmd == "sync-downloaded":
        output_root = sys.argv[2] if len(sys.argv) > 2 else "/app/files/output"
        result = sync_downloaded_flag(output_root)
        print(result)
    elif cmd == "rename-output":
        output_root = sys.argv[2] if len(sys.argv) > 2 else "/app/files/output"
        result = rename_output_dirs(output_root)
        print(result)
    else:
        create_tables()
        result = import_from_json(cmd if cmd != "import" else "emissions_and_docs.json")
        print(result)
