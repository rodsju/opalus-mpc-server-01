from __future__ import annotations

import os
from datetime import date
from typing import Any, Optional

import sqlparse
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Integer,
    MetaData,
    Table,
    Text,
    Numeric,
    and_,
    create_engine,
    func,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine

# ── Engine ────────────────────────────────────────────────────────────────────

_engine: Optional[Engine] = None


def _normalize_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    return url


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            _normalize_url(os.environ["DATABASE_URL"]),
            pool_pre_ping=True,
        )
    return _engine


# ── Tables (read-only, only the columns we query) ─────────────────────────────

metadata = MetaData()

emission = Table(
    "emission",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("emission_type", Text),
    Column("concentration", Text),
    Column("emission_name", Text),
    Column("total_emitted_volume", Numeric),
    Column("emission_code_name", Text),
    Column("date", Date),
    Column("mandate_date", Date),
    Column("downloaded_main_docs", Boolean),
)

emission_ambima = Table(
    "emission_ambima",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("emission_id", Integer),
    Column("estrategia", Text),
    Column("estrutura_cedente", Text),
    Column("estrutura_sacado", Text),
    Column("prazo_medio_ponderado", Text),
    Column("performance_do_credito", Text),
    Column("setor_do_cedente", Text),
    Column("tipo_de_lastro", Text),
    Column("natureza_do_direito_creditorio", Text),
    Column("garantias", Text),
    Column("coobrigacao_do_cedente", Text),
    Column("originacao", Text),
    Column("agencia_classificadora_de_risco", Text),
    Column("indice_de_subordinacao", Text),
    Column("observacoes_sobre_a_classificacao", Text),
    Column("class_review_need", Boolean),
    Column("tem_garantia", Text),
    Column("tem_subordinacao", Boolean),
)

emission_summary = Table(
    "emission_summary",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("emission_id", Integer),
    Column("h1", Text),
    Column("h2", Text),
    Column("h1_slug", Text),
    Column("h2_slug", Text),
    Column("content", Text),
)

emission_chunks = Table(
    "emission_chunks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("emission_id", Integer),
    Column("source_file", Text),
    Column("section_path", Text),
    Column("chunk_index", Integer),
    Column("chunk_text", Text),
    Column("char_count", Integer),
)

criterios_elegibilidade = Table(
    "criterios_elegibilidade",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("id_json", Text),
    Column("criterio_grupo", Text),
    Column("nivel_validacao", Text),
    Column("instrumento", Text),
    Column("clausula_sugerida", Text),
    Column("descricao_validacao", Text),
    Column("risco_id", Integer),
    Column("risco_mapeado", Text),
    Column("observacao", Text),
)


# ── Helpers ──────────────────────────────────────────────────────────────────

AMBIMA_DISTINCT_FIELDS = {
    "estrategia",
    "estrutura_cedente",
    "estrutura_sacado",
    "prazo_medio_ponderado",
    "performance_do_credito",
    "setor_do_cedente",
    "tipo_de_lastro",
    "natureza_do_direito_creditorio",
    "garantias",
    "coobrigacao_do_cedente",
    "originacao",
    "agencia_classificadora_de_risco",
    "indice_de_subordinacao",
    "observacoes_sobre_a_classificacao",
}


def _ambima_filter_clauses(filters: dict[str, Optional[str]]) -> list:
    clauses = []
    for field, value in filters.items():
        if value is None:
            continue
        col = emission_ambima.c[field]
        clauses.append(
            func.unaccent(func.lower(col)) == func.unaccent(func.lower(value))
        )
    return clauses


def _rows(result) -> list[dict]:
    return [dict(m) for m in result.mappings().all()]


def _to_date(value: Optional[str]) -> Optional[date]:
    if value is None or value == "":
        return None
    return date.fromisoformat(value)


def _check_emission_id(emission_id) -> tuple[Optional[int], Optional[dict]]:
    """Valida e converte emission_id para int.

    Retorna (valor, None) em sucesso ou (None, erro_dict) quando não numérico.
    """
    try:
        return int(emission_id), None
    except (TypeError, ValueError):
        return None, {
            "error": f"emission_id inválido: '{emission_id}'. O valor precisa ser numérico."
        }


# ── Queries ──────────────────────────────────────────────────────────────────

def count_emissions_by_group(emission_type: Optional[str] = None) -> dict:
    total = func.count().label("total")
    stmt = (
        select(
            emission.c.emission_type,
            emission.c.concentration,
            emission_ambima.c.estrategia,
            # emission_ambima.c.estrutura_cedente,
            # emission_ambima.c.estrutura_sacado,
            # emission_ambima.c.tipo_de_lastro,
            # emission_ambima.c.natureza_do_direito_creditorio,
            total,
        )
        .select_from(
            emission.outerjoin(
                emission_ambima, emission_ambima.c.emission_id == emission.c.id
            )
        )
        .where(emission.c.downloaded_main_docs.is_(True))
        .group_by(
            emission.c.emission_type,
            emission.c.concentration,
            emission_ambima.c.estrategia,
            # emission_ambima.c.estrutura_cedente,
            # emission_ambima.c.estrutura_sacado,
            # emission_ambima.c.tipo_de_lastro,
            # emission_ambima.c.natureza_do_direito_creditorio,
        )
        .order_by(
            emission.c.emission_type,
            emission.c.concentration,
            emission_ambima.c.estrategia,
            total.desc(),
        )
    )
    if emission_type:
        stmt = stmt.where(
            func.lower(emission.c.emission_type) == func.lower(emission_type)
        )

    with get_engine().connect() as conn:
        rows = _rows(conn.execute(stmt))
    return {"rows": rows, "count": len(rows)}


def list_available_emissions(
    emission_type: Optional[str] = None,
    emission_name: Optional[str] = None,
    emission_code_name: Optional[str] = None,
    mandate_date_from: Optional[str] = None,
    mandate_date_to: Optional[str] = None,
    estrategia: Optional[str] = None,
    # estrutura_cedente: Optional[str] = None,
    estrutura_sacado: Optional[str] = None,
    setor_do_cedente: Optional[str] = None,
    tipo_de_lastro: Optional[str] = None,
    garantias: Optional[str] = None,
    concentration: Optional[str] = None,
) -> dict:
    where = [emission.c.downloaded_main_docs.is_(True)]

    if emission_type:
        where.append(
            func.lower(emission.c.emission_type) == func.lower(emission_type)
        )
    if concentration:
        where.append(
            func.unaccent(func.lower(emission.c.concentration))
            == func.unaccent(func.lower(concentration))
        )
    if emission_name:
        where.append(emission.c.emission_name.ilike(f"%{emission_name}%"))
    if emission_code_name:
        where.append(emission.c.emission_code_name.ilike(f"%{emission_code_name}%"))
    if mandate_date_from:
        where.append(emission.c.mandate_date >= _to_date(mandate_date_from))
    if mandate_date_to:
        where.append(emission.c.mandate_date <= _to_date(mandate_date_to))

    where.extend(
        _ambima_filter_clauses(
            {
                "estrategia": estrategia,
                # "estrutura_cedente": estrutura_cedente,
                "estrutura_sacado": estrutura_sacado,
                "setor_do_cedente": setor_do_cedente,
                "tipo_de_lastro": tipo_de_lastro,
                "garantias": garantias,
            }
        )
    )

    stmt = (
        select(
            emission.c.id,
            emission.c.date,
            emission.c.emission_type,
            emission.c.emission_name,
            emission.c.emission_code_name,
            emission.c.concentration,
            emission.c.total_emitted_volume,
            emission.c.mandate_date,
            emission_ambima.c.estrategia,
            # emission_ambima.c.estrutura_cedente,
            emission_ambima.c.estrutura_sacado,
            emission_ambima.c.setor_do_cedente,
            emission_ambima.c.tipo_de_lastro,
            emission_ambima.c.natureza_do_direito_creditorio,
            emission_ambima.c.garantias,
            emission_ambima.c.coobrigacao_do_cedente,
            emission_ambima.c.originacao,
            # emission_ambima.c.agencia_classificadora_de_risco,
            # emission_ambima.c.indice_de_subordinacao,
        )
        .select_from(
            emission.outerjoin(
                emission_ambima, emission_ambima.c.emission_id == emission.c.id
            )
        )
        .where(and_(*where))
        .order_by(emission.c.emission_name)
    )

    with get_engine().connect() as conn:
        rows = _rows(conn.execute(stmt))
    return {"rows": rows, "count": len(rows)}


def list_emissions_parts() -> dict:
    sub = (
        select(
            emission_summary.c.h1,
            emission_summary.c.h1_slug,
            emission_summary.c.h2_slug,
        )
        .distinct()
        .subquery()
    )
    stmt = (
        select(
            sub.c.h1,
            sub.c.h1_slug,
            func.string_agg(sub.c.h2_slug, ",").label("h2_slugs_sections_topics"),
        )
        .group_by(sub.c.h1, sub.c.h1_slug)
        .order_by(sub.c.h1_slug)
    )
    with get_engine().connect() as conn:
        rows = _rows(conn.execute(stmt))
    return {"rows": rows, "count": len(rows)}


def get_emissions_part(
    h1_slug: Optional[str] = None,
    emission_id: Optional[str] = None,
    emission_type: Optional[str] = None,
    estrategia: Optional[str] = None,
    estrutura_cedente: Optional[str] = None,
    estrutura_sacado: Optional[str] = None,
    setor_do_cedente: Optional[str] = None,
    tipo_de_lastro: Optional[str] = None,
    natureza_do_direito_creditorio: Optional[str] = None,
    garantias: Optional[str] = None,
) -> dict:
    ambima_filters = {
        "estrategia": estrategia,
        "estrutura_cedente": estrutura_cedente,
        "estrutura_sacado": estrutura_sacado,
        "setor_do_cedente": setor_do_cedente,
        "tipo_de_lastro": tipo_de_lastro,
        "natureza_do_direito_creditorio": natureza_do_direito_creditorio,
        "garantias": garantias,
    }

    if not any([h1_slug, emission_id, emission_type, *ambima_filters.values()]):
        return {"error": "Informe ao menos um filtro"}

    where = []
    if h1_slug:
        where.append(emission_summary.c.h1_slug == h1_slug)
    if emission_id:
        eid, err = _check_emission_id(emission_id)
        if err:
            return err
        where.append(emission.c.id == eid)
    if emission_type:
        where.append(
            func.lower(emission.c.emission_type) == func.lower(emission_type)
        )
    where.extend(_ambima_filter_clauses(ambima_filters))

    stmt = (
        select(
            emission.c.id.label("emission_id"),
            emission.c.emission_type,
            emission.c.emission_name,
            emission.c.emission_code_name,
            emission.c.date,
            emission.c.mandate_date,
            emission_summary.c.h1,
            emission_summary.c.h2,
            emission_summary.c.content,
            emission_ambima.c.estrategia,
            emission_ambima.c.estrutura_cedente,
            emission_ambima.c.estrutura_sacado,
            emission_ambima.c.setor_do_cedente,
            emission_ambima.c.tipo_de_lastro,
            emission_ambima.c.natureza_do_direito_creditorio,
            emission_ambima.c.garantias,
 
        )
        .select_from(
            emission_summary.join(
                emission, emission.c.id == emission_summary.c.emission_id
            ).outerjoin(
                emission_ambima, emission_ambima.c.emission_id == emission.c.id
            )
        )
        .where(and_(*where))
        .order_by(
            emission.c.emission_name, emission_summary.c.h1, emission_summary.c.h2
        )
    )

    with get_engine().connect() as conn:
        rows = _rows(conn.execute(stmt))
    return {"rows": rows, "count": len(rows)}


def get_emission_chunk(
    emission_id: int, chunk_index: int, window: int = 0
) -> dict:
    eid, err = _check_emission_id(emission_id)
    if err:
        return err
    stmt = (
        select(
            emission_chunks.c.emission_id,
            emission_chunks.c.source_file,
            emission_chunks.c.section_path,
            emission_chunks.c.chunk_index,
            emission_chunks.c.chunk_text,
            emission_chunks.c.char_count,
        )
        .where(emission_chunks.c.emission_id == eid)
        .where(
            emission_chunks.c.chunk_index.between(
                chunk_index - window, chunk_index + window
            )
        )
        .order_by(emission_chunks.c.chunk_index)
    )
    with get_engine().connect() as conn:
        rows = _rows(conn.execute(stmt))
    return {"rows": rows, "count": len(rows)}


def get_emission_ambima(emission_id: int) -> dict:
    eid, err = _check_emission_id(emission_id)
    if err:
        return err
    stmt = select(
        emission_ambima.c.estrategia,
        emission_ambima.c.estrutura_cedente,
        emission_ambima.c.estrutura_sacado,
        emission_ambima.c.prazo_medio_ponderado,
        emission_ambima.c.performance_do_credito,
        emission_ambima.c.setor_do_cedente,
        emission_ambima.c.tipo_de_lastro,
        emission_ambima.c.natureza_do_direito_creditorio,
        emission_ambima.c.garantias,
        emission_ambima.c.coobrigacao_do_cedente,
        emission_ambima.c.originacao,
        emission_ambima.c.agencia_classificadora_de_risco,
        emission_ambima.c.indice_de_subordinacao,
        emission_ambima.c.observacoes_sobre_a_classificacao,
    ).where(emission_ambima.c.emission_id == eid)

    with get_engine().connect() as conn:
        row = conn.execute(stmt).mappings().first()
    if row is None:
        return {
            "error": f"Classificação ANBIMA não encontrada para emission_id {eid}"
        }
    return dict(row)


def get_emission_summary(
    emission_id: int, h1_slug: Optional[str] = None
) -> dict:
    eid, err = _check_emission_id(emission_id)
    if err:
        return err
    where = [emission_summary.c.emission_id == eid]
    if h1_slug:
        where.append(emission_summary.c.h1_slug == h1_slug)

    stmt = (
        select(
            emission_summary.c.h1,
            emission_summary.c.h1_slug,
            emission_summary.c.h2,
            emission_summary.c.h2_slug,
            emission_summary.c.content,
        )
        .where(and_(*where))
        .order_by(emission_summary.c.h1, emission_summary.c.h2)
    )
    with get_engine().connect() as conn:
        rows = _rows(conn.execute(stmt))
    return {"rows": rows, "count": len(rows)}


def list_criterios_elegibilidade(
    criterio_grupo: Optional[str] = None,
    nivel_validacao: Optional[str] = None,
    instrumento: Optional[str] = None,
    clausula_sugerida: Optional[str] = None,
    descricao_validacao: Optional[str] = None,
    min_similarity: float = 0.1,
) -> dict:
    where = []

    for field, value in (
        ("criterio_grupo", criterio_grupo),
        ("nivel_validacao", nivel_validacao),
        ("instrumento", instrumento),
    ):
        if value is not None:
            col = criterios_elegibilidade.c[field]
            where.append(
                func.unaccent(func.lower(col)) == func.unaccent(func.lower(value))
            )

    similarity_exprs = []
    for field, value in (
        ("clausula_sugerida", clausula_sugerida),
        ("descricao_validacao", descricao_validacao),
    ):
        if value is not None:
            col = criterios_elegibilidade.c[field]
            sim = func.similarity(
                func.unaccent(func.lower(col)),
                func.unaccent(func.lower(value)),
            )
            similarity_exprs.append((field, sim))
            where.append(sim >= min_similarity)

    select_cols = [
        # criterios_elegibilidade.c.id,
        criterios_elegibilidade.c.criterio_grupo,
        criterios_elegibilidade.c.nivel_validacao,
        criterios_elegibilidade.c.instrumento,
        criterios_elegibilidade.c.clausula_sugerida,
        criterios_elegibilidade.c.descricao_validacao,
        criterios_elegibilidade.c.risco_mapeado,
        criterios_elegibilidade.c.observacao,
    ]
    for field, sim in similarity_exprs:
        select_cols.append(sim.label(f"similarity_{field}"))

    stmt = select(*select_cols).where(and_(*where) if where else text("TRUE"))

    if similarity_exprs:
        if len(similarity_exprs) == 1:
            order_expr = similarity_exprs[0][1]
        else:
            order_expr = func.greatest(*[s for _, s in similarity_exprs])
        stmt = stmt.order_by(order_expr.desc())
    else:
        stmt = stmt.order_by(
            criterios_elegibilidade.c.criterio_grupo,
            criterios_elegibilidade.c.nivel_validacao,
            criterios_elegibilidade.c.id,
        )

    with get_engine().connect() as conn:
        rows = _rows(conn.execute(stmt))
    return {"rows": rows, "count": len(rows)}


def list_ambima_field_values(field: str) -> dict:
    if field not in AMBIMA_DISTINCT_FIELDS:
        return {
            "error": f"Campo inválido. Campos válidos: {sorted(AMBIMA_DISTINCT_FIELDS)}"
        }
    col = emission_ambima.c[field]
    stmt = select(col).where(col.is_not(None)).distinct().order_by(col)
    with get_engine().connect() as conn:
        values = [r[0] for r in conn.execute(stmt).all()]
    return {"field": field, "values": values, "count": len(values)}


# ── Mutations ────────────────────────────────────────────────────────────────

def flag_ambima_for_review(emission_id: int) -> dict:
    eid, err = _check_emission_id(emission_id)
    if err:
        return err
    stmt = (
        update(emission_ambima)
        .where(emission_ambima.c.emission_id == eid)
        .values(class_review_need=True)
    )
    with get_engine().begin() as conn:
        result = conn.execute(stmt)
    if result.rowcount == 0:
        return {
            "error": f"Classificação ANBIMA não encontrada para emission_id {eid}"
        }
    return {"ok": True, "emission_id": eid, "class_review_need": True}


TEM_GARANTIA_VALUES = {"não", "sim", "parcial"}


def set_ambima_tem_garantia(emission_id: int, value: str) -> dict:
    eid, err = _check_emission_id(emission_id)
    if err:
        return err
    if value not in TEM_GARANTIA_VALUES:
        return {
            "error": f"Valor inválido para tem_garantia. Use um de: {sorted(TEM_GARANTIA_VALUES)}"
        }
    stmt = (
        update(emission_ambima)
        .where(emission_ambima.c.emission_id == eid)
        .values(tem_garantia=value)
    )
    with get_engine().begin() as conn:
        result = conn.execute(stmt)
    if result.rowcount == 0:
        return {
            "error": f"Classificação ANBIMA não encontrada para emission_id {eid}"
        }
    return {"ok": True, "emission_id": eid, "tem_garantia": value}


def set_ambima_tem_subordinacao(emission_id: int, value: bool) -> dict:
    eid, err = _check_emission_id(emission_id)
    if err:
        return err
    stmt = (
        update(emission_ambima)
        .where(emission_ambima.c.emission_id == eid)
        .values(tem_subordinacao=value)
    )
    with get_engine().begin() as conn:
        result = conn.execute(stmt)
    if result.rowcount == 0:
        return {
            "error": f"Classificação ANBIMA não encontrada para emission_id {eid}"
        }
    return {"ok": True, "emission_id": eid, "tem_subordinacao": value}


# ── Diagnostic SQL tools ─────────────────────────────────────────────────────

FORBIDDEN = {"DELETE", "UPDATE", "INSERT", "DROP", "ALTER", "TRUNCATE"}


def is_safe_query(query: str) -> bool:
    q = query.strip().upper()
    if not q.startswith("SELECT"):
        return False
    parsed = sqlparse.parse(query)
    for stmt in parsed:
        for token in stmt.flatten():
            if token.value.upper() in FORBIDDEN:
                return False
    return True


def run_sql(query: str, limit: int = 100) -> dict:
    if not is_safe_query(query):
        return {"error": "Query não permitida"}

    query = query.strip().rstrip(";")
    if " LIMIT " not in query.upper():
        query = f"{query} LIMIT {limit}"

    with get_engine().connect() as conn:
        result = conn.execute(text(query))
        cols = list(result.keys())
        rows = [list(r) for r in result.all()]
    return {"columns": cols, "rows": rows, "count": len(rows)}


def list_tables() -> list[str]:
    stmt = text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'catalogo'"
    )
    with get_engine().connect() as conn:
        return [r[0] for r in conn.execute(stmt).all()]


def describe_table(table_name: str) -> list[tuple[Any, Any]]:
    stmt = text(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = :table_name"
    )
    with get_engine().connect() as conn:
        return [tuple(r) for r in conn.execute(stmt, {"table_name": table_name}).all()]
