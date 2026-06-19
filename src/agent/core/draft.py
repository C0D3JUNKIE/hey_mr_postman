"""Draft — strong-model reply drafting (§7.6).

Composes a brand-voiced reply grounded in retrieved KB chunks and the contact's
history. Returns a Draft (body + confidence + the KB chunks it was given). The
LLM client is injected; no SDK import here.
"""

from __future__ import annotations

import logging

from agent.core.enrich import Enrichment
from agent.core.models import Classification, Draft, Email, KBChunk

log = logging.getLogger(__name__)

_SYSTEM_TEMPLATE = """You are drafting a reply on behalf of the brand "{brand}".
Brand voice: {voice}

Rules:
- Reply ONLY using facts grounded in the provided KNOWLEDGE BASE and HISTORY.
  If the KB does not cover something, do not invent it — say you'll follow up.
- Match the brand voice. Be concise. No markdown, no subject line, no signature
  block beyond a simple sign-off — plain email body text only.
- Never promise refunds, legal outcomes, or billing changes; those are escalated.
- Address the customer's actual question first.

Respond with a SINGLE JSON object, no prose:
{{
  "body_text": "the reply body",
  "confidence": float 0.0-1.0 — how well-grounded and complete this reply is
}}"""


def _format_kb(chunks: list[KBChunk]) -> str:
    if not chunks:
        return "(no KB chunks retrieved)"
    return "\n\n".join(
        f"[{i + 1}] source={c.source} score={c.score:.3f}\n{c.text}"
        for i, c in enumerate(chunks)
    )


def _format_history(enrichment: Enrichment) -> str:
    if not enrichment.history:
        return "(no prior interactions — likely a new contact)"
    lines = []
    for it in enrichment.history[:10]:
        ts = it.ts.isoformat() if it.ts else "?"
        lines.append(f"- {ts} [{it.direction.value}] {it.summary}")
    return "\n".join(lines)


def draft_reply(
    email: Email,
    classification: Classification,
    enrichment: Enrichment,
    *,
    brand_voice: str,
    llm,
    model: str,
) -> Draft:
    """Produce a grounded brand-voiced draft. `llm` exposes complete_json()."""
    system = _SYSTEM_TEMPLATE.format(brand=email.brand or "the company", voice=brand_voice or "professional")

    body = (email.body_text or "").strip()
    if len(body) > 6000:
        body = body[:6000] + "\n…[truncated]"

    user = (
        f"CUSTOMER MESSAGE\n"
        f"From: {email.true_sender}\nSubject: {email.subject}\n\n{body}\n\n"
        f"CLASSIFICATION: category={classification.category.value} "
        f"sentiment={classification.sentiment.value} language={classification.language}\n\n"
        f"CONTACT HISTORY:\n{_format_history(enrichment)}\n\n"
        f"KNOWLEDGE BASE:\n{_format_kb(enrichment.kb_chunks)}\n\n"
        f"Write the reply now in {classification.language}."
    )

    data = llm.complete_json(model=model, system=system, user=user, max_tokens=1500)

    body_text = str(data.get("body_text", "")).strip()
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    if not body_text:
        log.warning("drafting model returned empty body for %s", email.message_id)
        confidence = 0.0

    return Draft(body_text=body_text, confidence=confidence, used_kb_chunks=enrichment.kb_chunks)