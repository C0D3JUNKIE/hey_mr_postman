"""Enrich — CRM history + KB retrieval (§7.5).

Pulls the contact's recent interactions and brand-scoped KB chunks. Pure core:
talks only to CRMPort and KnowledgePort.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.core.models import Classification, Contact, Email, Interaction, KBChunk
from agent.ports.crm import CRMPort
from agent.ports.knowledge import KnowledgePort


@dataclass
class Enrichment:
    contact: Contact
    history: list[Interaction] = field(default_factory=list)
    kb_chunks: list[KBChunk] = field(default_factory=list)


def _retrieval_query(email: Email, classification: Classification) -> str:
    subject = (email.subject or "").strip()
    body = (email.body_text or "").strip()
    return f"{subject}\n{body}"[:1000]


def enrich(
    email: Email,
    classification: Classification,
    crm: CRMPort,
    knowledge: KnowledgePort,
    *,
    history_limit: int = 10,
    k: int = 5,
) -> Enrichment:
    """Look up (or create) the contact, fetch history, retrieve KB grounding."""
    contact = crm.find_contact(email.true_sender)
    if contact is None:
        contact = crm.upsert_contact(
            Contact(email=email.true_sender, name=None, brand=email.brand)
        )

    history: list[Interaction] = []
    if contact.id:
        history = crm.history(contact.id, limit=history_limit)

    kb_chunks: list[KBChunk] = []
    if email.brand:
        kb_chunks = knowledge.retrieve(
            email.brand, _retrieval_query(email, classification), k=k
        )

    return Enrichment(contact=contact, history=history, kb_chunks=kb_chunks)