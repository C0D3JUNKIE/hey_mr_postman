"""Pre-filter — rule-based junk / loop / auto-reply drop (§7.3, §13.2).

Runs BEFORE any LLM call to protect cost and quality. Pure functions over an
Email; no I/O. A dropped message is moved to No-Action/ by the pipeline.

Loop prevention is a safety non-negotiable: we must never reply to no-reply
addresses, mailing lists, auto-responders, or our own forwarders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent.core.models import Email

# Local-parts / patterns we must never reply to (loop prevention).
_NOREPLY_PATTERNS = (
    "no-reply",
    "noreply",
    "do-not-reply",
    "donotreply",
    "mailer-daemon",
    "postmaster",
    "bounce",
    "notifications",
    "automated",
)

# Headers that mark bulk / automated / vacation mail. Presence ⇒ drop.
# (header_name_lower, optional value substring that must match; None = any value)
_AUTO_HEADERS: tuple[tuple[str, str | None], ...] = (
    ("auto-submitted", None),          # RFC 3834; "no" means human, handled below
    ("x-auto-response-suppress", None),
    ("list-id", None),
    ("list-unsubscribe", None),
    ("precedence", "bulk"),
    ("precedence", "list"),
    ("precedence", "junk"),
    ("x-autoreply", None),
    ("x-autorespond", None),
)

_SUBJECT_AUTO_PATTERNS = (
    r"^\s*auto(?:matic)?[- ]?reply",
    r"out of (?:the )?office",
    r"\bvacation\b",
    r"delivery status notification",
    r"undeliverable",
    r"mail delivery failed",
)


@dataclass
class PrefilterResult:
    drop: bool
    reason: str | None = None


def _addr_is_noreply(addr: str | None) -> bool:
    if not addr:
        return False
    local = addr.split("@", 1)[0].lower()
    return any(p in local for p in _NOREPLY_PATTERNS)


def prefilter(email: Email, *, extra_headers: dict[str, str] | None = None) -> PrefilterResult:
    """Decide whether to drop a message before classification.

    `extra_headers` is an optional case-insensitive dict of raw MIME headers the
    transport adapter captured (List-Id, Auto-Submitted, Precedence, ...). The
    core stays transport-agnostic; the adapter supplies what it parsed.
    """
    headers = {k.lower(): (v or "") for k, v in (extra_headers or {}).items()}

    # 1) Never reply to no-reply / daemon / our own forwarders.
    if _addr_is_noreply(email.true_sender) or _addr_is_noreply(email.from_addr):
        return PrefilterResult(True, "no-reply / automated sender")

    # 2) Auto-submitted: explicit "no" is fine (human); anything else is automated.
    auto_submitted = headers.get("auto-submitted", "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return PrefilterResult(True, f"auto-submitted: {auto_submitted}")

    # 3) Mailing-list / bulk headers.
    for name, needle in _AUTO_HEADERS:
        if name == "auto-submitted":
            continue  # handled above
        if name in headers:
            val = headers[name].lower()
            if needle is None or needle in val:
                return PrefilterResult(True, f"bulk/list header: {name}")

    # 4) Subject heuristics for OOO / vacation / bounce.
    subj = email.subject or ""
    for pat in _SUBJECT_AUTO_PATTERNS:
        if re.search(pat, subj, re.IGNORECASE):
            return PrefilterResult(True, f"auto-reply subject: {subj!r}")

    # 5) Empty / contentless messages with no actionable body.
    if not (email.body_text or "").strip() and not (email.body_html or "").strip():
        return PrefilterResult(True, "empty body")

    return PrefilterResult(False)