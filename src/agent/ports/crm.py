"""CRMPort — contact + interaction memory.

v1 adapter: internal SQLite tables. Future: HubSpot, Salesforce, Pipedrive.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.core.models import Contact, Interaction


@runtime_checkable
class CRMPort(Protocol):
    def find_contact(self, email: str) -> Contact | None:
        ...

    def upsert_contact(self, contact: Contact) -> Contact:
        """Create or update by email; returns the persisted contact (with id)."""
        ...

    def log_interaction(self, contact_id: str, event: Interaction) -> None:
        ...

    def history(self, contact_id: str, limit: int = 10) -> list[Interaction]:
        """Most-recent-first interaction history for enrichment."""
        ...

    def create_lead(self, contact_id: str, details: dict) -> None:
        """Stub OK in v1 — seam for external CRM lead creation."""
        ...