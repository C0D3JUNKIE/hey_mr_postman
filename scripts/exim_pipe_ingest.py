"""cPanel Exim pipe-to-program receiver (optional v1, §9 `pipe` ingest mode).

cPanel can pipe a raw message to a program on delivery. This script reads the
raw RFC822 message on stdin and hands it to the agent's pipeline as a single
RawMessage — no IMAP polling needed.

Configure in cPanel (Forwarders → "Pipe to a Program") to invoke:
    /path/to/.venv/bin/python -m scripts.exim_pipe_ingest

The message is processed immediately and then the source mailbox copy (if any)
is left to the normal IMAP lifecycle. This path does not assign an IMAP UID, so
folder moves are skipped; idempotency relies on messages.message_id.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent.core.models import RawMessage

# Override with the SCENARIO env var when invoked by cPanel.
DEFAULT_SCENARIO = "config/scenarios/example.yaml"


def main() -> int:
    load_dotenv()
    # Import here so a misconfigured env fails loudly but only when invoked.
    from scripts.run_agent import App, load_scenario

    raw_bytes = sys.stdin.buffer.read()
    if not raw_bytes.strip():
        print("exim_pipe_ingest: empty stdin", file=sys.stderr)
        return 1

    config = load_scenario(os.environ.get("SCENARIO", DEFAULT_SCENARIO))
    app = App(config)
    raw = RawMessage(uid=f"pipe-{uuid.uuid4().hex}", folder="INBOX", raw_bytes=raw_bytes)
    state = app.pipeline.process(raw)
    print(f"exim_pipe_ingest: outcome={state.get('outcome')}")
    # Exit 0 so Exim considers delivery successful.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())