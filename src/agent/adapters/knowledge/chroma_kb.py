"""Chroma knowledge-base adapter — KnowledgePort (§3, §7.5).

Per-brand grounding KB on a local Chroma persistent client. One collection per
brand keeps retrieval brand-scoped. Behind the port so a hosted vector DB can
replace it without touching enrich/draft.

This is an ADAPTER: importing chromadb here is fine; the core never does.
"""

from __future__ import annotations

import logging

import chromadb

from agent.core.models import KBChunk

log = logging.getLogger(__name__)


def _collection_name(brand: str) -> str:
    # Chroma collection names: 3-63 chars, alnum/_/-, start+end alnum.
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in brand.lower())
    return f"kb_{safe}"[:63]


class ChromaKnowledge:
    """KnowledgePort over a local persistent Chroma client.

    Uses Chroma's default embedding function (local ONNX MiniLM) so v1 needs no
    external embedding API. Distance is converted to a similarity-ish score.
    """

    def __init__(self, persist_path: str):
        self.client = chromadb.PersistentClient(path=persist_path)

    def _collection(self, brand: str):
        return self.client.get_or_create_collection(name=_collection_name(brand))

    # ── KnowledgePort ──

    def retrieve(self, brand: str, query: str, k: int = 5) -> list[KBChunk]:
        if not query.strip():
            return []
        col = self._collection(brand)
        if col.count() == 0:
            return []
        res = col.query(query_texts=[query], n_results=min(k, col.count()))
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        chunks: list[KBChunk] = []
        for doc, meta, dist in zip(docs, metas, dists):
            meta = meta or {}
            chunks.append(
                KBChunk(
                    brand=brand,
                    text=doc,
                    source=str(meta.get("source", "unknown")),
                    score=_dist_to_score(dist),
                )
            )
        return chunks

    # ── ingestion (used by scripts/ingest, not part of the port) ──

    def ingest(self, brand: str, texts: list[str], sources: list[str], ids: list[str]) -> int:
        if not texts:
            return 0
        col = self._collection(brand)
        col.upsert(
            documents=texts,
            metadatas=[{"source": s, "brand": brand} for s in sources],
            ids=ids,
        )
        return len(texts)


def _dist_to_score(distance: float | None) -> float:
    """Map a (lower-is-better) distance to a 0-1 score. Heuristic, monotonic."""
    if distance is None:
        return 0.0
    return round(1.0 / (1.0 + float(distance)), 4)