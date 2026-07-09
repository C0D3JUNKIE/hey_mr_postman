"""Anthropic client wrappers (§2).

Two call sites: cheap/fast triage (Haiku) and stronger drafting (Sonnet).
Model ids are config values (ModelsConfig), never hardcoded here — the wrapper
just takes whatever model string it is handed.

Kept deliberately thin: the core never imports this directly; it talks to
classify/draft which receive an LLMClient. This keeps the vendor SDK at the edge.
"""

from __future__ import annotations

import json
import logging

from anthropic import Anthropic

log = logging.getLogger(__name__)


class LLMClient:
    """Thin wrapper over the Anthropic Messages API."""

    def __init__(self, api_key: str | None = None):
        # api_key=None lets the SDK read ANTHROPIC_API_KEY from the env.
        self._client = Anthropic(api_key=api_key) if api_key else Anthropic()

    def complete(
        self,
        model: str,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> str:
        """Single-turn completion. Returns the concatenated text content."""
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        ).strip()

    def complete_json(
        self,
        model: str,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> dict:
        """Completion that must return a single JSON object. Parsed strictly (§7.4).

        The system prompt instructs the model to emit a single JSON object; we
        trim any stray prose to the outermost ``{...}`` and parse. We do NOT
        prefill the assistant turn — modern models (Sonnet 4.6+, Opus 4.6+,
        Fable 5) reject a trailing assistant message with a 400. Raises
        ValueError if the result isn't valid JSON.
        """
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[
                {"role": "user", "content": user},
            ],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        )
        try:
            return json.loads(_trim_to_json(text))
        except json.JSONDecodeError as e:
            log.error("LLM did not return valid JSON: %s\n---\n%s", e, text)
            raise ValueError(f"expected JSON from {model}, got: {text[:500]}") from e


def _trim_to_json(text: str) -> str:
    """Trim to the outermost {...} so trailing prose after the object is ignored."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]