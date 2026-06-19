"""Parse tests — MIME normalization + forwarded-sender extraction (§6, §15)."""

from __future__ import annotations

from agent.adapters.transport.imap_smtp import parse_raw, resolve_true_sender
from tests.conftest import load_eml


def test_reply_to_wins_for_forwarded_form(config):
    """The real customer is in Reply-To, not the From (website system)."""
    email = parse_raw(load_eml("forwarded_replyto.eml"), uid="1", folder="INBOX", config=config)
    assert email.from_addr == "wordpress@siteA.com"
    assert email.true_sender == "jane.customer@gmail.com"  # not the forwarder
    assert email.brand == "siteA"
    assert email.to_addr == "support@siteA.com"
    assert "cracked" in email.body_text


def test_body_fallback_when_no_reply_to(config):
    """No Reply-To → recover the real address from the form-mailer body."""
    email = parse_raw(load_eml("forwarded_bodyonly.eml"), uid="2", folder="INBOX", config=config)
    assert email.from_addr == "noreply@siteB.com"
    assert email.true_sender == "bob.buyer@outlook.com"
    assert email.brand == "siteB"


def test_direct_html_threading(config):
    """multipart/alternative: text extracted, threading headers parsed."""
    email = parse_raw(load_eml("direct_html.eml"), uid="3", folder="INBOX", config=config)
    assert email.true_sender == "carol@example.org"
    assert email.in_reply_to == "<order-4521@siteA.com>"
    assert email.references == ["<order-4521@siteA.com>"]
    assert email.body_html is not None
    assert "ship" in email.body_text


def test_resolve_true_sender_priority():
    # Reply-To beats From and body.
    assert resolve_true_sender("sys@site.com", "real@x.com", "Email: other@y.com") == "real@x.com"
    # No reply-to → body pattern.
    assert resolve_true_sender("sys@site.com", None, "Email: real@y.com") == "real@y.com"
    # Nothing → From.
    assert resolve_true_sender("sys@site.com", None, "no address here") == "sys@site.com"


def test_message_id_and_received(config):
    email = parse_raw(load_eml("forwarded_replyto.eml"), uid="1", folder="INBOX", config=config)
    assert email.message_id == "<form-0001@siteA.com>"
    assert email.received_at is not None