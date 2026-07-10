"""Scenario config — load + validate a tenant bundle (§12).

A scenario YAML bundles everything that defines one deployment: transport,
sending identities, brands, autonomy rules, retention, model ids. Validated by
pydantic. Secrets are NEVER in config — only the *name* of the env var that
holds each secret (the `*_env` fields), resolved at adapter-construction time.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


# ─────────────────────────── transport ────────────────────────────


class FoldersConfig(BaseModel):
    replied: str = "Replied"
    awaiting: str = "Awaiting-Approval"
    escalated: str = "Escalated"
    no_action: str = "No-Action"
    trash: str = "Trash"


class ImapConfig(BaseModel):
    host: str
    port: int = 993
    ssl: bool = True
    username: str
    password_env: str
    inbox: str = "INBOX"
    ingest_mode: str = "poll"  # poll | idle | pipe
    poll_interval_seconds: int = 60
    folders: FoldersConfig = Field(default_factory=FoldersConfig)

    @field_validator("ingest_mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        if v not in {"poll", "idle", "pipe"}:
            raise ValueError(f"ingest_mode must be poll|idle|pipe, got {v!r}")
        return v


class TransportConfig(BaseModel):
    type: str = "imap_smtp"
    imap: ImapConfig


class SendingIdentity(BaseModel):
    """Match an incoming to_addr → which mailbox to authenticate as for the reply."""

    match_to: str
    host: str
    port: int = 465
    username: str
    password_env: str


# ─────────────────────────── brand / autonomy ────────────────────────────


class KBSource(BaseModel):
    """A web source to scrape into a brand's KB (used by scripts/scrape_site).

    Declared per brand so a refresh is `scrape_site --brand X` with no URLs —
    the sources (and their auth/crawl settings) come from config.
    """

    url: str
    crawl: bool = False
    max_pages: int = 50
    auth_user: str | None = None
    auth_pass_env: str | None = None  # env var name holding the basic-auth password


class BrandConfig(BaseModel):
    kb_path: str
    voice: str = ""
    sources: list[KBSource] = Field(default_factory=list)


class AutonomyConfig(BaseModel):
    mode: str = "draft_only"  # draft_only | approval | auto
    auto_send_allowlist: list[str] = Field(default_factory=list)
    confidence_threshold: float = 0.8
    human_required_categories: list[str] = Field(
        default_factory=lambda: ["billing", "legal", "refund"]
    )
    # Recipient addresses (the account the customer wrote *to*, e.g.
    # legal@brand.com) that always require a human, regardless of category or
    # confidence — higher-stakes identities than routine customer service.
    human_required_identities: list[str] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def _valid_autonomy_mode(cls, v: str) -> str:
        if v not in {"draft_only", "approval", "auto"}:
            raise ValueError(f"autonomy.mode must be draft_only|approval|auto, got {v!r}")
        return v


class RetentionConfig(BaseModel):
    trash_grace_days: int = 30
    offload_attachments: bool = True


class SLAConfig(BaseModel):
    """Follow-up timer (§7.10): nudge if a queued draft sits unactioned too long."""

    enabled: bool = True
    # Hours a draft may wait in the approval queue before it breaches SLA.
    follow_up_hours: float = 24.0


class DigestConfig(BaseModel):
    """Daily digest (§14 Phase 6): periodic activity summary for the operator."""

    enabled: bool = True
    # Lookback window the digest summarizes.
    window_hours: float = 24.0


class ModelsConfig(BaseModel):
    triage: str = "claude-haiku-4-5-20251001"
    draft: str = "claude-sonnet-4-6"


class StorageConfig(BaseModel):
    """Local paths for v1 zero-setup stores. Postgres-compatible schema lives in store/."""

    db_path: str = "data/agent.db"
    chroma_path: str = "data/chroma"
    attachments_path: str = "data/attachments"


# ─────────────────────────── top-level scenario ────────────────────────────


class ScenarioConfig(BaseModel):
    scenario: str
    transport: TransportConfig
    sending_identities: list[SendingIdentity] = Field(default_factory=list)
    brands: dict[str, BrandConfig] = Field(default_factory=dict)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    sla: SLAConfig = Field(default_factory=SLAConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    # ── helpers ──

    def identity_for(self, to_addr: str) -> SendingIdentity | None:
        """Match an inbound recipient to the brand mailbox we reply *from* (§9)."""
        target = (to_addr or "").strip().lower()
        for ident in self.sending_identities:
            if ident.match_to.strip().lower() == target:
                return ident
        return None

    def kill_switch_engaged(self) -> bool:
        """Global kill switch (§13.7): truthy env flag forces draft_only."""
        raw = os.environ.get("KILL_SWITCH", "").strip().lower()
        return raw not in {"", "0", "false", "no", "off"}

    def effective_mode(self) -> str:
        """Autonomy mode after applying the kill switch."""
        return "draft_only" if self.kill_switch_engaged() else self.autonomy.mode


def load_scenario(path: str | Path) -> ScenarioConfig:
    """Load and validate a scenario YAML into a ScenarioConfig."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"scenario config not found: {p}")
    data = yaml.safe_load(p.read_text()) or {}
    return ScenarioConfig.model_validate(data)


def resolve_secret(env_name: str) -> str:
    """Resolve a *_env reference to its value, failing loudly if unset (§13.5)."""
    val = os.environ.get(env_name)
    if not val:
        raise RuntimeError(
            f"required secret env var {env_name!r} is unset. "
            "Set it in your environment / .env (never in config)."
        )
    return val


# Forward-ref resolution for TransportConfig.imap
TransportConfig.model_rebuild()