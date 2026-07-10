"""Scrape web pages into a brand's KB as markdown (KB source tool).

This is an *ingestion-time* helper, not part of the runtime pipeline: it fetches
HTML, converts it to clean markdown, and writes one file per page into the
brand's ``kb_path``. Review the markdown, then load it with ``ingest_kb``.

Two ways to name what to scrape:

    # (a) config-driven — sources declared per brand in the scenario YAML:
    python -m scripts.scrape_site --scenario config/scenarios/oneeleven.yaml \
        --brand squadchatter --prune
    python -m scripts.ingest_kb --scenario config/scenarios/oneeleven.yaml \
        --brand squadchatter --replace

    # (b) ad-hoc — URLs on the CLI (uses the global --auth-*/--crawl flags):
    python -m scripts.scrape_site --brand squadchatter \
        --auth-user devsquad --auth-pass-env SQUADCHATTER_DEV_PASSWORD \
        --crawl https://squadchatter.com/

Keeping scrape → review → ingest as separate steps means the markdown is a
reviewable artifact (git-diffable) before it ever influences a drafted reply.
Each run reports which pages are new / CHANGED / unchanged so a human can catch
a pricing or policy change before it goes live.

Freshness: ``--prune`` deletes kb files for pages that disappeared from the
site, and ``ingest_kb --replace`` rebuilds the vectors authoritatively — together
they guarantee removed/shrunk content leaves no stale chunks behind.

Auth: config sources carry ``auth_user`` + ``auth_pass_env``; in CLI mode pass
``--auth-user`` plus ``--auth-pass-env`` (env var *name*, per the project's
``*_env`` secret convention). ``--auth-pass`` allows a throwaway dev password inline.

Crawl: without ``crawl``/``--crawl`` only the named URLs are fetched. Crawl
follows same-host ``<a href>`` links (bounded by ``max_pages``), staying within
the directory prefix of each seed so it won't wander the whole host.

Stdlib only — no new dependencies (mirrors ingest_kb.py).
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent.config import load_scenario  # noqa: E402

DEFAULT_SCENARIO = "config/scenarios/example.yaml"

# Tags whose content is chrome/markup, not KB prose.
_SKIP_CONTAINERS = {
    "script", "style", "svg", "head", "nav", "footer", "noscript", "button",
}
_BLOCK = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "summary", "hr",
    "div", "section", "ul", "ol", "tr",
}
_HTML_SUFFIXES = {"", ".html", ".htm", ".php"}


# ─────────────────────────── HTML → markdown ───────────────────────────
class _Converter(HTMLParser):
    """Extract main-body prose from a page as lightweight markdown.

    Keeps headings, paragraphs, list items, FAQ <details>/<summary>, and link
    text (mailto addresses are appended). Drops nav/footer/scripts/svg.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.out: list[str] = []
        self.cur: list[str] = []
        self.tag_stack: list[str] = []
        self.href: str | None = None
        self.links: list[str] = []  # collected hrefs (for crawl)

    def _flush(self, prefix: str = "") -> None:
        text = re.sub(r"[ \t]+", " ", "".join(self.cur)).strip()
        if text:
            self.out.append(prefix + text)
        self.cur = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_CONTAINERS:
            self.skip_depth += 1
            return
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if self.skip_depth:
            return
        self.tag_stack.append(tag)
        if tag in _BLOCK:
            self._flush()
        if tag == "a":
            self.href = dict(attrs).get("href")
        if tag == "br":
            self.cur.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag == "hr" and not self.skip_depth:
            self._flush()
            self.out.append("---")

    def handle_endtag(self, tag):
        if tag in _SKIP_CONTAINERS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "summary":
            text = re.sub(r"[ \t]+", " ", "".join(self.cur)).strip()
            self.cur = []
            if text:
                self.out.append(f"**Q: {text}**")
        elif tag in _BLOCK:
            prefix = {
                "h1": "# ", "h2": "## ", "h3": "### ",
                "h4": "#### ", "h5": "#### ", "h6": "#### ",
                "li": "- ",
            }.get(tag, "")
            self._flush(prefix)
        if tag == "a":
            self.href = None
        if self.tag_stack and self.tag_stack[-1] == tag:
            self.tag_stack.pop()

    def handle_data(self, data):
        if self.skip_depth or not data.strip():
            if not data.strip() and self.cur and not self.cur[-1].endswith(" "):
                self.cur.append(" ")
            return
        self.cur.append(data)
        if self.href and self.href.startswith("mailto:"):
            addr = self.href[len("mailto:"):]
            if addr and addr not in data:
                self.cur.append(f" ({addr})")

    def result(self) -> str:
        self._flush()
        text = "\n\n".join(self.out)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"


def _html_to_markdown(html: str) -> tuple[str, list[str]]:
    c = _Converter()
    c.feed(html)
    return c.result(), c.links


# ─────────────────────────── fetch / crawl ───────────────────────────
def _ssl_context(insecure: bool) -> ssl.SSLContext:
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:  # prefer certifi's CA bundle — the stdlib default often can't find one on macOS
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _fetch(url: str, auth_header: str | None, timeout: int, ctx: ssl.SSLContext) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "heymrpostman-kb-scraper/1"})
    if auth_header:
        req.add_header("Authorization", auth_header)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.read()
    if "html" not in ctype.lower() and ctype:
        raise ValueError(f"not HTML (Content-Type: {ctype})")
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    if m:
        charset = m.group(1)
    return raw.decode(charset, errors="ignore")


def _canonical(url: str) -> str:
    """Drop fragment + query for dedup/naming."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _within_prefix(url: str, seed: str) -> bool:
    s, u = urlsplit(seed), urlsplit(url)
    if s.netloc != u.netloc:
        return False
    prefix = s.path.rsplit("/", 1)[0] + "/"
    return u.path.startswith(prefix)


def _filename_for(url: str) -> str:
    path = urlsplit(url).path
    if not path or path.endswith("/"):
        name = (path.strip("/").replace("/", "__") or "index")
    else:
        name = path.strip("/").replace("/", "__")
        name = re.sub(r"\.(html?|php)$", "", name, flags=re.I)
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", name) or "index"
    return f"{name}.md"


def _looks_like_page(url: str) -> bool:
    path = urlsplit(url).path
    suffix = Path(path).suffix.lower()
    return suffix in _HTML_SUFFIXES


def _auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def _resolve_sources(args, brand_cfg, err) -> list[dict]:
    """Build the list of {url, crawl, max_pages, auth} to scrape.

    CLI URLs (with the global --auth-*/--crawl flags) take precedence; with no
    URLs, fall back to the brand's declared ``sources`` in the scenario YAML.
    """
    sources: list[dict] = []
    if args.urls:
        header = None
        if args.auth_user:
            password = args.auth_pass or (
                os.environ.get(args.auth_pass_env) if args.auth_pass_env else None
            )
            if not password:
                err("--auth-user given but no password (set --auth-pass-env / --auth-pass)")
            header = _auth_header(args.auth_user, password)
        for u in args.urls:
            sources.append(
                {"url": _canonical(u), "crawl": args.crawl,
                 "max_pages": args.max_pages, "auth": header}
            )
        return sources

    if not brand_cfg.sources:
        err(f"no URLs given and brand {args.brand!r} has no `sources:` in config")
    for s in brand_cfg.sources:
        header = None
        if s.auth_user:
            password = os.environ.get(s.auth_pass_env) if s.auth_pass_env else None
            if not password:
                err(f"source {s.url}: env var {s.auth_pass_env!r} is unset/empty")
            header = _auth_header(s.auth_user, password)
        sources.append(
            {"url": _canonical(s.url), "crawl": s.crawl,
             "max_pages": s.max_pages, "auth": header}
        )
    return sources


def main(argv=None):
    p = argparse.ArgumentParser(description="Scrape web pages into a brand's KB as markdown")
    p.add_argument("urls", nargs="*", help="seed URL(s); omit to use the brand's config sources")
    p.add_argument("--scenario", default=DEFAULT_SCENARIO)
    p.add_argument("--brand", required=True)
    p.add_argument("--out", default=None, help="override output dir (default: brand kb_path)")
    p.add_argument("--auth-user", default=None, help="HTTP basic-auth username (CLI-URL mode)")
    p.add_argument("--auth-pass-env", default=None, help="env var holding the basic-auth password")
    p.add_argument("--auth-pass", default=None, help="basic-auth password inline (dev only)")
    p.add_argument("--crawl", action="store_true", help="follow same-host links under each seed's dir")
    p.add_argument("--max-pages", type=int, default=50)
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--insecure", action="store_true", help="skip TLS cert verification (dev sites only)")
    p.add_argument("--prune", action="store_true", help="delete kb files for pages not seen this run")
    args = p.parse_args(argv)

    config = load_scenario(args.scenario)
    brand_cfg = config.brands.get(args.brand)
    if brand_cfg is None:
        p.error(f"unknown brand {args.brand!r}; known: {list(config.brands)}")
    out_dir = Path(args.out or brand_cfg.kb_path)
    sources = _resolve_sources(args, brand_cfg, p.error)

    # Snapshot existing KB files so we can report changes and detect orphans.
    existing: dict[str, str] = {}
    if out_dir.exists():
        for f in out_dir.glob("*.md"):
            existing[f.name] = f.read_text()

    ctx = _ssl_context(args.insecure)
    produced: dict[str, str] = {}  # filename -> new content (dedups "/" vs "/index.html")
    seen: set[str] = set()
    out_dir.mkdir(parents=True, exist_ok=True)

    for src in sources:
        seed, queue, count = src["url"], [src["url"]], 0
        while queue and count < src["max_pages"]:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            fname = _filename_for(url)
            if fname in produced:
                continue
            try:
                html = _fetch(url, src["auth"], args.timeout, ctx)
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
                print(f"skip    {url}  ({e})", file=sys.stderr)
                continue
            md, links = _html_to_markdown(html)
            status = "new" if fname not in existing else (
                "same" if existing[fname] == md else "CHANGED"
            )
            produced[fname] = md
            (out_dir / fname).write_text(md)
            count += 1
            print(f"{status:7} {fname}  <- {url}")

            if src["crawl"]:
                for href in links:
                    nxt = _canonical(urljoin(url, href))
                    if not nxt.startswith(("http://", "https://")):
                        continue
                    if nxt in seen or nxt in queue or not _looks_like_page(nxt):
                        continue
                    if _within_prefix(nxt, seed):
                        queue.append(nxt)
        if src["crawl"] and count >= src["max_pages"]:
            print(f"note: {seed} hit --max-pages={src['max_pages']}; some links unfetched",
                  file=sys.stderr)

    # Change summary + orphan handling.
    changed = sorted(n for n in produced if n in existing and existing[n] != produced[n])
    added = sorted(n for n in produced if n not in existing)
    orphans = sorted(n for n in existing if n not in produced)
    print(
        f"\n{len(produced)} page(s) -> {out_dir}  "
        f"({len(added)} new, {len(changed)} changed, "
        f"{len(produced) - len(added) - len(changed)} unchanged)"
    )
    if changed:
        print("  changed: " + ", ".join(changed))
    if orphans:
        if args.prune:
            for n in orphans:
                (out_dir / n).unlink()
            print(f"  pruned {len(orphans)} orphaned file(s): " + ", ".join(orphans))
        else:
            print("  orphaned (on disk, not on site this run): " + ", ".join(orphans)
                  + "  — re-run with --prune to remove")
    print(
        f"next: python -m scripts.ingest_kb --scenario {args.scenario} "
        f"--brand {args.brand} --replace"
    )


if __name__ == "__main__":
    main()