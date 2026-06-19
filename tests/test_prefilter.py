"""Pre-filter tests — loop/auto-reply/bulk drop before any LLM call (§7.3, §13.2)."""

from __future__ import annotations

from agent.adapters.transport.imap_smtp import extract_raw_headers, parse_raw
from agent.core.models import Email
from agent.core.prefilter import prefilter
from tests.conftest import load_eml


def _email(**kw) -> Email:
    base = dict(
        uid="1",
        message_id="<m@x>",
        from_addr="a@b.com",
        true_sender="a@b.com",
        to_addr="support@siteA.com",
        subject="Hello",
        body_text="I need help with my order.",
    )
    base.update(kw)
    return Email(**base)


def test_genuine_message_passes():
    assert prefilter(_email()).drop is False


def test_noreply_sender_dropped():
    res = prefilter(_email(true_sender="no-reply@service.com"))
    assert res.drop is True


def test_mailer_daemon_dropped():
    res = prefilter(_email(from_addr="mailer-daemon@x.com", true_sender="mailer-daemon@x.com"))
    assert res.drop is True


def test_empty_body_dropped():
    assert prefilter(_email(body_text="", body_html=None)).drop is True


def test_ooo_subject_dropped():
    assert prefilter(_email(subject="Automatic reply: Out of Office")).drop is True


def test_auto_submitted_header_dropped(config):
    raw = load_eml("autoreply_ooo.eml")
    email = parse_raw(raw, uid="4", folder="INBOX", config=config)
    headers = extract_raw_headers(raw)
    res = prefilter(email, extra_headers=headers)
    assert res.drop is True


def test_mailing_list_dropped(config):
    raw = load_eml("mailing_list.eml")
    email = parse_raw(raw, uid="5", folder="INBOX", config=config)
    headers = extract_raw_headers(raw)
    res = prefilter(email, extra_headers=headers)
    assert res.drop is True


def test_auto_submitted_no_is_human():
    # Auto-Submitted: no means a human sent it — must NOT drop.
    res = prefilter(_email(), extra_headers={"Auto-Submitted": "no"})
    assert res.drop is False