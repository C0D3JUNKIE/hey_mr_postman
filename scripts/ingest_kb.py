"""KB ingestion — load brand documents into the Chroma knowledge base (Phase 3).

Reads plain-text / markdown files under a brand's kb_path and upserts them
(chunked) into the per-brand Chroma collection.

Usage:
    python -m scripts.ingest_kb --brand siteA
    python -m scripts.ingest_kb --brand siteA --path kb/siteA
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent.adapters.knowledge.chroma_kb import ChromaKnowledge
from agent.config import load_scenario

DEFAULT_SCENARIO = "config/scenarios/example.yaml"
_CHUNK_CHARS = 1200
_OVERLAP = 150


def _chunk(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= _CHUNK_CHARS:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = start + _CHUNK_CHARS
        chunks.append(text[start:end])
        start = end - _OVERLAP
    return chunks


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ingest brand KB into Chroma")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    parser.add_argument("--brand", required=True)
    parser.add_argument("--path", default=None, help="override brand kb_path")
    parser.add_argument("--glob", default="**/*", help="file glob within the path")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="drop the brand's existing vectors first (authoritative rebuild — "
        "prevents stale chunks from removed/shrunk docs)",
    )
    args = parser.parse_args(argv)

    config = load_scenario(args.scenario)
    brand_cfg = config.brands.get(args.brand)
    if brand_cfg is None:
        parser.error(f"unknown brand {args.brand!r}; known: {list(config.brands)}")

    kb_dir = Path(args.path or brand_cfg.kb_path)
    if not kb_dir.exists():
        parser.error(f"kb path does not exist: {kb_dir}")

    kb = ChromaKnowledge(config.storage.chroma_path)
    if args.replace:
        kb.reset(args.brand)
    texts, sources, ids = [], [], []
    for f in sorted(kb_dir.glob(args.glob)):
        if not f.is_file() or f.suffix.lower() not in {".txt", ".md", ".markdown"}:
            continue
        content = f.read_text(errors="ignore")
        for i, chunk in enumerate(_chunk(content)):
            cid = hashlib.sha1(f"{f}-{i}".encode()).hexdigest()
            texts.append(chunk)
            sources.append(str(f))
            ids.append(cid)

    n = kb.ingest(args.brand, texts, sources, ids)
    print(f"ingested {n} chunk(s) for brand {args.brand} from {kb_dir}")


if __name__ == "__main__":
    main()