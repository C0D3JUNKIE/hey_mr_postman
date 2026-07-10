"""Routing tests — autonomy decision table (§8)."""

from __future__ import annotations

import pytest

from agent.config import AutonomyConfig
from agent.core.models import Category, Classification, Draft, Priority, RouteAction, Sentiment
from agent.core.route import decide_route


def _cls(**kw) -> Classification:
    base = dict(
        category=Category.FAQ,
        priority=Priority.NORMAL,
        sentiment=Sentiment.NEUTRAL,
        language="en",
        needs_human=False,
        confidence=0.95,
    )
    base.update(kw)
    return Classification(**base)


def _draft() -> Draft:
    return Draft(body_text="Here is your answer.", confidence=0.9)


@pytest.fixture
def autonomy() -> AutonomyConfig:
    return AutonomyConfig(
        mode="auto",
        auto_send_allowlist=["faq", "acknowledgement"],
        confidence_threshold=0.8,
        human_required_categories=["billing", "legal", "refund"],
    )


def test_draft_only_never_auto_sends(autonomy):
    res = decide_route(_cls(), _draft(), autonomy, effective_mode="draft_only")
    assert res.action == RouteAction.APPROVE


def test_approval_mode_queues(autonomy):
    res = decide_route(_cls(), _draft(), autonomy, effective_mode="approval")
    assert res.action == RouteAction.APPROVE


def test_auto_allowlisted_high_confidence_sends(autonomy):
    res = decide_route(_cls(category=Category.FAQ, confidence=0.95), _draft(), autonomy,
                       effective_mode="auto")
    assert res.action == RouteAction.AUTO_SEND


def test_low_confidence_escalates(autonomy):
    res = decide_route(_cls(confidence=0.4), _draft(), autonomy, effective_mode="auto")
    assert res.action == RouteAction.ESCALATE


def test_negative_sentiment_escalates(autonomy):
    res = decide_route(_cls(sentiment=Sentiment.NEGATIVE), _draft(), autonomy,
                       effective_mode="auto")
    assert res.action == RouteAction.ESCALATE


def test_billing_always_escalates(autonomy):
    res = decide_route(_cls(category=Category.BILLING), _draft(), autonomy, effective_mode="auto")
    assert res.action == RouteAction.ESCALATE


def test_non_allowlisted_category_needs_approval(autonomy):
    res = decide_route(_cls(category=Category.SALES, confidence=0.95), _draft(), autonomy,
                       effective_mode="auto")
    assert res.action == RouteAction.APPROVE


def test_needs_human_flag_escalates(autonomy):
    res = decide_route(_cls(needs_human=True), _draft(), autonomy, effective_mode="auto")
    assert res.action == RouteAction.ESCALATE


def test_human_required_identity_escalates_even_when_auto_sendable(autonomy):
    """A high-stakes account (legal@) must escalate even for an allowlisted,
    high-confidence category that would otherwise auto-send."""
    autonomy.human_required_identities = ["legal@brand.com"]
    res = decide_route(
        _cls(category=Category.FAQ, confidence=0.99), _draft(), autonomy,
        effective_mode="auto", to_addr="Legal@Brand.com",  # case-insensitive
    )
    assert res.action == RouteAction.ESCALATE
    assert "identity" in res.reason


def test_non_gated_identity_still_auto_sends(autonomy):
    autonomy.human_required_identities = ["legal@brand.com"]
    res = decide_route(
        _cls(category=Category.FAQ, confidence=0.95), _draft(), autonomy,
        effective_mode="auto", to_addr="support@brand.com",
    )
    assert res.action == RouteAction.AUTO_SEND


def test_identity_gate_absent_by_default(autonomy):
    """No configured identities → to_addr has no effect (back-compat)."""
    res = decide_route(
        _cls(category=Category.FAQ, confidence=0.95), _draft(), autonomy,
        effective_mode="auto", to_addr="legal@brand.com",
    )
    assert res.action == RouteAction.AUTO_SEND