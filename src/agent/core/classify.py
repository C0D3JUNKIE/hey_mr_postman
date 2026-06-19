"""Classify — cheap-model triage → Classification (§7.4).

Calls the triage model (Haiku) and parses STRICT JSON into a Classification.
The LLM client is injected; the core never imports the Anthropic SDK directly.
"""

from __future__ import annotations

import logging

from agent.core.models import Category, Classification, Email, Priority, Sentiment

log = logging.getLogger(__name__)

_SYSTEM = """You are an email triage classifier for a small-business support inbox.
Classify the message and respond with a SINGLE JSON object, no prose, no markdown.

Schema (all fields required):
{
  "category": one of ["faq","acknowledgement","support","sales","billing","legal","refund","spam","other"],
  "priority": one of ["low","normal","high","urgent"],
  "sentiment": one of ["positive","neutral","negative"],
  "language": ISO 639-1 code (e.g. "en","es"),
  "needs_human": boolean — true if a human must handle this (sensitive/ambiguous/legal),
  "confidence": float 0.0-1.0 — your confidence in the category
}

Guidance:
- billing, legal, and refund ALWAYS warrant needs_human=true.
- Angry or threatening tone → sentiment "negative" and needs_human=true.
- If unsure of the category, use "other" with a low confidence."""


def _build_user_prompt(email: Email) -> str:
    body = (email.body_text or "").strip()
    if len(body) > 6000:
        body = body[:6000] + "\n…[truncated]"
    return (
        f"From (true sender): {email.true_sender}\n"
        f"To: {email.to_addr}\n"
        f"Brand: {email.brand or 'unknown'}\n"
        f"Subject: {email.subject}\n\n"
        f"Body:\n{body}"
    )


def classify(email: Email, llm, model: str) -> Classification:
    """Classify an email. `llm` is an LLMClient-like with complete_json()."""
    data = llm.complete_json(model=model, system=_SYSTEM, user=_build_user_prompt(email))
    return _coerce(data)


def _coerce(data: dict) -> Classification:
    """Defensively coerce raw model JSON into a validated Classification."""

    def _enum(value, enum_cls, default):
        try:
            return enum_cls(str(value).strip().lower())
        except (ValueError, AttributeError):
            log.warning("classifier returned invalid %s: %r", enum_cls.__name__, value)
            return default

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    category = _enum(data.get("category"), Category, Category.OTHER)
    needs_human = bool(data.get("needs_human", False))
    # Enforce the spec invariant regardless of what the model said.
    if category in (Category.BILLING, Category.LEGAL, Category.REFUND):
        needs_human = True

    return Classification(
        category=category,
        priority=_enum(data.get("priority"), Priority, Priority.NORMAL),
        sentiment=_enum(data.get("sentiment"), Sentiment, Sentiment.NEUTRAL),
        language=str(data.get("language", "en"))[:8] or "en",
        needs_human=needs_human,
        confidence=confidence,
    )