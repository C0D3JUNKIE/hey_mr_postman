"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import (
    BrandConfig,
    ImapConfig,
    ScenarioConfig,
    TransportConfig,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_eml(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def config() -> ScenarioConfig:
    """A minimal in-memory scenario with two brands, no secrets required."""
    return ScenarioConfig(
        scenario="test",
        transport=TransportConfig(
            type="imap_smtp",
            imap=ImapConfig(
                host="mail.test",
                username="hub@test",
                password_env="HUB_IMAP_PASSWORD",
            ),
        ),
        brands={
            "siteA": BrandConfig(kb_path="kb/siteA", voice="friendly"),
            "siteB": BrandConfig(kb_path="kb/siteB", voice="warm"),
        },
    )