"""KB freshness: declarative sources parse, and reset() rebuilds authoritatively.

The reset() test guards the stale-chunk failure mode: upsert never deletes, so a
shrunk/removed page would otherwise leave orphaned chunks (e.g. an old price)
serving forever. reset() before ingest is what makes a refresh authoritative.
"""

from __future__ import annotations

from agent.adapters.knowledge.chroma_kb import ChromaKnowledge
from agent.config import BrandConfig


def test_brand_sources_parse_from_config():
    brand = BrandConfig.model_validate(
        {
            "kb_path": "kb/x",
            "sources": [
                {
                    "url": "https://x.example/",
                    "crawl": True,
                    "auth_user": "dev",
                    "auth_pass_env": "X_DEV_PASSWORD",
                }
            ],
        }
    )
    assert len(brand.sources) == 1
    src = brand.sources[0]
    assert src.url == "https://x.example/"
    assert src.crawl is True
    assert src.max_pages == 50  # default
    assert src.auth_pass_env == "X_DEV_PASSWORD"


def test_brand_sources_default_empty():
    assert BrandConfig(kb_path="kb/x").sources == []


def test_reset_drops_stale_chunks(tmp_path):
    kb = ChromaKnowledge(str(tmp_path / "chroma"))
    # A 4-chunk page; chunk 2 carries a price that will later be removed.
    kb.ingest("b", ["c0", "c1", "PRICE $3.99", "c3"], ["p"] * 4,
              [f"page-{i}" for i in range(4)])
    assert kb.client.get_collection("kb_b").count() == 4

    # Plain re-ingest of a shrunk (2-chunk) page leaves the stale price behind.
    kb.ingest("b", ["c0", "c1"], ["p"] * 2, [f"page-{i}" for i in range(2)])
    docs = kb.client.get_collection("kb_b").get()["documents"]
    assert any("3.99" in d for d in docs)  # orphan survives — the bug

    # reset() before ingest makes the rebuild authoritative: orphan is gone.
    kb.reset("b")
    kb.ingest("b", ["c0", "c1"], ["p"] * 2, [f"page-{i}" for i in range(2)])
    col = kb.client.get_collection("kb_b")
    assert col.count() == 2
    assert not any("3.99" in d for d in col.get()["documents"])


def test_reset_missing_collection_is_noop(tmp_path):
    kb = ChromaKnowledge(str(tmp_path / "chroma"))
    kb.reset("never-ingested")  # must not raise