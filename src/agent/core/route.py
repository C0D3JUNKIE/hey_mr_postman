"""Route — the autonomy decision: send vs approve vs escalate (§8).

Pure function of (classification, draft, autonomy config, effective mode). No
I/O. The pipeline turns the RouteAction into an interrupt or a send.

Decision table:
- Kill switch / draft_only mode → never auto-send; everything queues for approval.
- Always escalate: negative sentiment, low confidence, or human-required category.
- Auto-send only if: mode == auto AND category in allowlist AND confidence >=
  threshold AND category not human-required AND not needs_human.
- Otherwise → approval.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.config import AutonomyConfig
from agent.core.models import Category, Classification, Draft, RouteAction, Sentiment


@dataclass
class RouteResult:
    action: RouteAction
    reason: str


def decide_route(
    classification: Classification,
    draft: Draft,
    autonomy: AutonomyConfig,
    *,
    effective_mode: str,
) -> RouteResult:
    """Compute the routing decision. `effective_mode` already accounts for the
    kill switch (see ScenarioConfig.effective_mode)."""

    human_required = {c.lower() for c in autonomy.human_required_categories}
    allowlist = {c.lower() for c in autonomy.auto_send_allowlist}
    cat = classification.category.value
    conf = classification.confidence

    # ── always-escalate conditions (§8) ──
    if classification.sentiment == Sentiment.NEGATIVE:
        return RouteResult(RouteAction.ESCALATE, "negative sentiment")
    if cat in human_required or classification.category in (
        Category.BILLING,
        Category.LEGAL,
        Category.REFUND,
    ):
        return RouteResult(RouteAction.ESCALATE, f"human-required category: {cat}")
    if conf < autonomy.confidence_threshold:
        return RouteResult(
            RouteAction.ESCALATE,
            f"low confidence {conf:.2f} < {autonomy.confidence_threshold:.2f}",
        )
    if classification.needs_human:
        return RouteResult(RouteAction.ESCALATE, "classifier flagged needs_human")

    # ── shadow / approval modes never auto-send ──
    if effective_mode in ("draft_only", "approval"):
        return RouteResult(RouteAction.APPROVE, f"mode={effective_mode}")

    # ── auto mode: gated per-category ──
    if cat in allowlist and conf >= autonomy.confidence_threshold:
        return RouteResult(RouteAction.AUTO_SEND, f"auto-send allowed for {cat} @ {conf:.2f}")

    return RouteResult(RouteAction.APPROVE, "auto not permitted for this category")