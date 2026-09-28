from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import psycopg
from dotenv import load_dotenv

load_dotenv()

from src.vectors import (
    build_qwen3_http_embedding_fn,
    vector_to_pgvector_str,
)

TOKEN        = os.environ["CHUTES_API_TOKEN"]
TOKEN        = os.environ["OPENROUTER_API_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
EMBEDDING_URL = os.getenv("CHUTES_EMBEDDINGS_URL", "https://chutes-qwen-qwen3-embedding-8b.chutes.ai/v1/embeddings")
EMBEDDING_URL = os.getenv("OPENROUTER_EMBEDDINGS_URL", "https://openrouter.ai/api/v1/embeddings")
QUERY_INSTRUCTION = (
    "Represent this query for retrieving relevant legal and financial information "
    "from Portuguese legal and structured finance documents."
)
MODEL_ID_CHUTES = "Qwen/Qwen3-Embedding-8B"
MODEL_ID_OPENROUTER = "qwen/qwen3-embedding-8b"

# ── Embedding da query ────────────────────────────────────────────────────────

def build_query_embed_fn():
    return build_qwen3_http_embedding_fn(
        api_url=EMBEDDING_URL,
        api_key=TOKEN,
        model=MODEL_ID_OPENROUTER,
        instruction=QUERY_INSTRUCTION,
        encoding_format="float",
        timeout=120,
    )


def embed_query(query_text: str, embed_fn) -> List[float]:
    vectors = embed_fn([query_text])
    if not vectors or not vectors[0]:
        raise ValueError("Falha ao gerar embedding da query")
    return vectors[0]


# ── Busca vetorial ────────────────────────────────────────────────────────────

def search_chunks(
    query_text: str,
    embed_fn,
    db_url: str = DATABASE_URL,
    emission_id: Optional[int] = None,
    top_k: int = 5,
    min_similarity: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Busca simples por similaridade na tabela emission_chunks."""
    embedding = embed_query(query_text, embed_fn)
    embedding_pg = vector_to_pgvector_str(embedding)

    where_clauses = []
    params: Dict[str, Any] = {"embedding": embedding_pg, "top_k": top_k}

    if emission_id is not None:
        where_clauses.append("emission_id = %(emission_id)s")
        params["emission_id"] = emission_id

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT
            id,
            emission_id,
            source_file,
            chunk_index,
            chunk_text,
            char_count,
            1 - (embedding <=> %(embedding)s::vector) AS similarity
        FROM emission_chunks
        {where_sql}
        ORDER BY embedding <=> %(embedding)s::vector
        LIMIT %(top_k)s
    """

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    if min_similarity is not None:
        rows = [r for r in rows if float(r["similarity"]) >= min_similarity]

    return rows


def search_chunks_across_emissions(
    query_text: str,
    embed_fn,
    db_url: str = DATABASE_URL,
    emission_type: Optional[str] = None,
    min_similarity: float = 0.5,
    chunks_per_emission: int = 2,
    candidate_limit: int = 500,
) -> List[Dict[str, Any]]:
    """Busca semântica em todas as emissões, limitando a N chunks por emissão.

    Estratégia: consulta candidatos por `min_similarity` (sem top_k por emissão),
    ordena por similaridade e em Python seleciona até `chunks_per_emission` por emissão.
    """
    embedding = embed_query(query_text, embed_fn)
    embedding_pg = vector_to_pgvector_str(embedding)

    where_clauses = ["1 - (ec.embedding <=> %(embedding)s::vector) >= %(min_similarity)s"]
    params: Dict[str, Any] = {
        "embedding": embedding_pg,
        "min_similarity": min_similarity,
        "candidate_limit": candidate_limit,
    }

    if emission_type is not None:
        where_clauses.append("lower(e.emission_type) = lower(%(emission_type)s)")
        params["emission_type"] = emission_type

    where_sql = "WHERE " + " AND ".join(where_clauses)

    sql = f"""
        SELECT
            ec.id,
            ec.emission_id,
            e.emission_name,
            e.emission_code_name,
            ec.source_file,
            ec.chunk_index,
            ec.chunk_text,
            ec.char_count,
            e.emission_type,
            1 - (ec.embedding <=> %(embedding)s::vector) AS similarity
        FROM emission_chunks ec
        JOIN emission e ON e.id = ec.emission_id
        {where_sql}
        ORDER BY ec.embedding <=> %(embedding)s::vector
        LIMIT %(candidate_limit)s
    """

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    by_emission: Dict[Any, int] = {}
    selected: List[Dict[str, Any]] = []
    for row in rows:
        eid = row["emission_id"]
        if by_emission.get(eid, 0) >= chunks_per_emission:
            continue
        by_emission[eid] = by_emission.get(eid, 0) + 1
        selected.append(row)

    return selected


# ── Reranking heurístico (opcional) ──────────────────────────────────────────

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().strip())


def rerank(
    results: List[Dict[str, Any]],
    query: str,
    keywords: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Reordena resultados com boost de keywords e penalidades simples."""
    query_norm = _normalize(query)
    query_terms = [w for w in re.split(r"\W+", query_norm) if len(w) >= 4]
    keywords = keywords or []

    for item in results:
        score = float(item.get("similarity", 0.0))
        text  = _normalize(item.get("chunk_text", ""))

        if len(text) < 80:
            score -= 0.08

        term_hits = sum(1 for w in query_terms if w in text)
        score += min(term_hits * 0.01, 0.05)

        for kw in keywords:
            if _normalize(kw) in text:
                score += 0.03

        item["_score"] = score

    results.sort(key=lambda x: x["_score"], reverse=True)
    return results


# ── Contexto RAG ──────────────────────────────────────────────────────────────

def build_rag_context(results: List[Dict[str, Any]]) -> str:
    parts = []
    for i, item in enumerate(results, 1):
        similarity = round(float(item.get("similarity", 0)), 4)
        parts.append(
            f"[Trecho {i}] emission_id={item.get('emission_id')} | "
            f"similaridade={similarity}\n"
            f"{item.get('chunk_text', '').strip()}"
        )
    return "\n\n".join(parts)


# ── CLI simples ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="Texto da pergunta")
    parser.add_argument("--emission-id", type=int, default=None, help="Filtrar por emission_id")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-similarity", type=float, default=None)
    args = parser.parse_args()

    print(f"Query: {args.query}")
    if args.emission_id:
        print(f"emission_id: {args.emission_id}")
    print()

    embed_fn = build_query_embed_fn()
    results  = search_chunks(
        args.query,
        embed_fn,
        emission_id=args.emission_id,
        top_k=args.top_k,
        min_similarity=args.min_similarity,
    )
    results = rerank(results, args.query)

    for i, r in enumerate(results, 1):
        print(f"[{i}] score={r.get('_score', 0):.4f} | emission_id={r['emission_id']} | chunk_index={r['chunk_index']}")
        print(f"     {r['chunk_text'][:200].strip()}")
        print()
