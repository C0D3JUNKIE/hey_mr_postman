"""TransportPort — reading & sending mail.

v1 adapter: IMAP/SMTP (cPanel). Future: Gmail API, Graph, webhook/queue.
Ports depend on nothing but domain models.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.core.models import Email, OutgoingEmail, RawMessage


@runtime_checkable
class TransportPort(Protocol):
    def read_new(self) -> list[RawMessage]:
        """Pull un-actioned mail from INBOX (e.g. SEARCH UNSEEN), as UIDs."""
        ...

    def parse(self, raw: RawMessage) -> Email:
        """MIME → normalized Email, resolving true_sender / brand / threading."""
        ...

    def send(self, reply: OutgoingEmail) -> None:
        """Send as the matched brand identity, with threading headers set."""
        ...

    def move(self, msg_uid: str, folder: str) -> None:
        """Move a message out of INBOX into a status folder."""
        ...

    def mark_processed(self, msg_uid: str) -> None:
        """Idempotency marker (flag and/or move). Already-handled mail is skipped."""
        ...