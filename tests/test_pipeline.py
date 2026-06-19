"""End-to-end pipeline test with stub adapters (§15 acceptance criteria).

Proves the full flow without network or LLM: a forwarded contact-form email is
parsed (real customer recovered), classified, persisted, drafted, and queued for
approval — nothing auto-sends in draft_only. On approval it sends from the
matched brand identity, moves to Replied/, and writes an audit row. Re-running
does not reprocess.
"""

from __future__ import annotations

import pytest

from agent.adapters.crm.internal_db import InternalDbCRM
from agent.adapters.notification.web_queue import WebQueueNotifier
from agent.adapters.transport.imap_smtp import extract_raw_headers, parse_raw
from agent.config import (
    AutonomyConfig,
    BrandConfig,
    ImapConfig,
    ScenarioConfig,
    SendingIdentity,
    StorageConfig,
    TransportConfig,
)
from agent.core.models import ApprovalDecision, Category, DecisionType, RawMessage
from agent.core.pipeline import AgentPipeline
from agent.store.db import Database
from agent.store.repos import AuditLog, MessageRepo
from tests.conftest import load_eml


# ── stub adapters ──

class FakeTransport:
    """Records sends/moves; parses real bytes via the real parser."""

    def __init__(self, config):
        self.config = config
        self.sent = []
        self.moved = []
        self.marked = []
        self._queue: list[RawMessage] = []

    def feed(self, raw: RawMessage):
        self._queue.append(raw)

    def read_new(self):
        q, self._queue = self._queue, []
        return q

    def parse(self, raw):
        return parse_raw(raw.raw_bytes, raw.uid, raw.folder, self.config)

    def raw_headers(self, raw):
        return extract_raw_headers(raw.raw_bytes)

    def send(self, outgoing):
        self.sent.append(outgoing)

    def move(self, uid, folder):
        self.moved.append((uid, folder))

    def mark_processed(self, uid):
        self.marked.append(uid)


class FakeKnowledge:
    def retrieve(self, brand, query, k=5):
        return []


class FakeLLM:
    """Deterministic stand-in for the Anthropic client."""

    def complete_json(self, model, system, user, **kw):
        if "classifier" in system or "triage" in system:
            return {
                "category": "support",
                "priority": "normal",
                "sentiment": "neutral",
                "language": "en",
                "needs_human": False,
                "confidence": 0.9,
            }
        return {"body_text": "Thanks for reaching out — we can help with that.", "confidence": 0.9}

    def complete(self, *a, **k):
        return ""


@pytest.fixture
def app_config():
    return ScenarioConfig(
        scenario="test",
        transport=TransportConfig(
            type="imap_smtp",
            imap=ImapConfig(host="mail.test", username="hub@test", password_env="HUB_IMAP_PASSWORD"),
        ),
        sending_identities=[
            SendingIdentity(
                match_to="support@siteA.com", host="mail.siteA.com",
                username="support@siteA.com", password_env="SITEA_SUPPORT_PASSWORD",
            )
        ],
        brands={"siteA": BrandConfig(kb_path="kb/siteA", voice="friendly")},
        autonomy=AutonomyConfig(mode="draft_only"),
        storage=StorageConfig(db_path=":memory:"),
    )


@pytest.fixture
def harness(app_config, tmp_path):
    db = Database(tmp_path / "agent.db")
    transport = FakeTransport(app_config)
    crm = InternalDbCRM(db)
    notifier = WebQueueNotifier(db)
    messages = MessageRepo(db)
    audit = AuditLog(db)
    pipeline = AgentPipeline(
        config=app_config, transport=transport, crm=crm, knowledge=FakeKnowledge(),
        notifier=notifier, llm=FakeLLM(), message_repo=messages, audit=audit,
    )
    return dict(db=db, transport=transport, crm=crm, notifier=notifier,
               messages=messages, audit=audit, pipeline=pipeline)


def test_draft_only_queues_nothing_sends(harness):
    raw = RawMessage(uid="100", folder="INBOX", raw_bytes=load_eml("forwarded_replyto.eml"))
    state = harness["pipeline"].process(raw)

    # parsed real customer, not forwarder
    assert state["email"].true_sender == "jane.customer@gmail.com"
    assert state["outcome"] == "awaiting"
    # nothing auto-sent in draft_only
    assert harness["transport"].sent == []
    # moved out of INBOX to Awaiting-Approval
    assert harness["transport"].moved == [("100", "Awaiting-Approval")]
    # contact created
    assert harness["crm"].find_contact("jane.customer@gmail.com") is not None
    # one pending approval queued
    pending = harness["notifier"].list_pending()
    assert len(pending) == 1
    assert pending[0].classification.category == Category.SUPPORT


def test_approval_sends_from_brand_identity(harness):
    raw = RawMessage(uid="101", folder="INBOX", raw_bytes=load_eml("forwarded_replyto.eml"))
    harness["pipeline"].process(raw)
    approval = harness["notifier"].list_pending()[0]

    outcome = harness["pipeline"].process_approval(
        approval, ApprovalDecision(decision=DecisionType.SEND, actor="alice")
    )
    assert outcome == "replied"
    sent = harness["transport"].sent
    assert len(sent) == 1
    # sent from the brand identity matched on the original recipient domain
    assert sent[0].from_identity == "support@siteA.com"
    assert sent[0].to_addr == "jane.customer@gmail.com"
    assert sent[0].in_reply_to == approval.message_id

    # audit row for the send exists
    rows = harness["db"].query_all("SELECT action FROM audit_log WHERE action = 'send'")
    assert len(rows) == 1


def test_idempotent_no_reprocess(harness):
    raw = RawMessage(uid="102", folder="INBOX", raw_bytes=load_eml("forwarded_replyto.eml"))
    harness["pipeline"].process(raw)
    n_before = len(harness["notifier"].list_pending())
    # second pass with same Message-ID
    raw2 = RawMessage(uid="103", folder="INBOX", raw_bytes=load_eml("forwarded_replyto.eml"))
    state = harness["pipeline"].process(raw2)
    assert state["outcome"] == "no_action"
    assert "duplicate" in state["reason"]
    assert len(harness["notifier"].list_pending()) == n_before


def test_prefiltered_mail_no_action(harness):
    raw = RawMessage(uid="104", folder="INBOX", raw_bytes=load_eml("mailing_list.eml"))
    state = harness["pipeline"].process(raw)
    assert state["outcome"] == "no_action"
    assert harness["transport"].sent == []
    assert harness["notifier"].list_pending() == []