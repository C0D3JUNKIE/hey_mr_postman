"""KnowledgePort — per-brand grounding knowledge base.

v1 adapter: Chroma local. Future: hosted vector DB.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.core.models import KBChunk


@runtime_checkable
class KnowledgePort(Protocol):
    def retrieve(self, brand: str, query: str, k: int = 5) -> list[KBChunk]:
        """Top-k brand-scoped grounding chunks for a query."""
        ...