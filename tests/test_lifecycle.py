"""Phase 6 lifecycle jobs: attachment offload, trash expunge, quota warning (§10).

These exercise the maintenance orchestration + the transport adapter's offload /
parse helpers with no live IMAP — a FakeTransport stands in where a connection
would be needed.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from agent import maintenance
from agent.adapters.transport.imap_smtp import ImapSmtpTransport, parse_raw
from agent.config import StorageConfig
from agent.core.models import RawMessage
from agent.store.db import Database
from agent.store.repos import MessageRepo


# ─────────────────────────── helpers / fakes ───────────────────────────


def _raw_with_attachment(
    message_id: str = "<att1@x>",
    filename: str = "report.pdf",
    blob: bytes = b"%PDF-1.4 fake pdf bytes",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = "cust@example.com"
    msg["To"] = "support@siteA.com"
    msg["Subject"] = "Please see attached"
    msg["Message-ID"] = message_id
    msg.set_content("body text")
    msg.add_attachment(blob, maintype="application", subtype="pdf", filename=filename)
    return msg.as_bytes()


class FakeExpungeTransport:
    """Records expunge_older_than calls; returns a configurable count."""

    def __init__(self, count: int = 3):
        self.count = count
        self.calls: list[tuple[str, int]] = []

    def expunge_older_than(self, folder: str, days: int) -> int:
        self.calls.append((folder, days))
        return self.count


class FakeQuotaTransport:
    def __init__(self, usage):
        self._usage = usage

    def mailbox_usage(self):
        return self._usage


# ─────────────────────────── attachment offload ───────────────────────────


def test_offload_attachment_writes_blob(config, tmp_path):
    config.storage = StorageConfig(attachments_path=str(tmp_path / "att"))
    ref = maintenance.offload_attachment(config, "<m@x>", "doc.txt", b"hello")
    from pathlib import Path

    assert Path(ref).is_file()
    assert Path(ref).read_bytes() == b"hello"


def test_transport_offload_sets_storage_ref(config, tmp_path):
    config.storage = StorageConfig(attachments_path=str(tmp_path / "att"))
    transport = ImapSmtpTransport(config)
    raw_bytes = _raw_with_attachment(filename="report.pdf", blob=b"%PDF-1.4 fake pdf bytes")
    email = parse_raw(raw_bytes, "1", "INBOX", config)

    assert len(email.attachments) == 1
    assert email.attachments[0].storage_ref is None  # parse is metadata-only

    transport.offload_attachments(email, raw_bytes)

    from pathlib import Path

    ref = email.attachments[0].storage_ref
    assert ref is not None
    assert Path(ref).read_bytes() == b"%PDF-1.4 fake pdf bytes"


def test_record_attachments_persists_and_is_idempotent(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.execute(
        "INSERT INTO messages (id, uid, message_id) VALUES (?, ?, ?)",
        ("mid", "1", "<m@x>"),
    )
    messages = MessageRepo(db)
    raw_bytes = _raw_with_attachment(message_id="<m@x>")
    from agent.config import (
        ImapConfig,
        ScenarioConfig,
        TransportConfig,
    )

    cfg = ScenarioConfig(
        scenario="t",
        transport=TransportConfig(imap=ImapConfig(host="h", username="u", password_env="P")),
    )
    email = parse_raw(raw_bytes, "1", "INBOX", cfg)
    email.attachments[0].storage_ref = "/store/report.pdf"

    messages.record_attachments("<m@x>", email.attachments)
    messages.record_attachments("<m@x>", email.attachments)  # re-run

    rows = db.query_all("SELECT filename, storage_ref FROM attachments WHERE message_id = ?", ("<m@x>",))
    assert len(rows) == 1  # idempotent, not duplicated
    assert rows[0]["filename"] == "report.pdf"
    assert rows[0]["storage_ref"] == "/store/report.pdf"


# ─────────────────────────── trash expunge ───────────────────────────


def test_expunge_trash_drives_transport(config):
    transport = FakeExpungeTransport(count=5)
    n = maintenance.expunge_trash(config, transport)
    assert n == 5
    # called with the configured Trash folder + grace days
    assert transport.calls == [("Trash", config.retention.trash_grace_days)]


def test_expunge_trash_noop_without_support(config):
    class NoExpunge:
        pass

    assert maintenance.expunge_trash(config, NoExpunge()) == 0


# ─────────────────────────── quota warning ───────────────────────────


def test_warn_on_quota_warns_over_threshold(config):
    transport = FakeQuotaTransport((95, 100))  # 95% used
    assert maintenance.warn_on_quota(config, transport, threshold=0.9) is True


def test_warn_on_quota_quiet_under_threshold(config):
    transport = FakeQuotaTransport((10, 100))  # 10% used
    assert maintenance.warn_on_quota(config, transport, threshold=0.9) is False


def test_warn_on_quota_none_usage(config):
    transport = FakeQuotaTransport(None)
    assert maintenance.warn_on_quota(config, transport) is False