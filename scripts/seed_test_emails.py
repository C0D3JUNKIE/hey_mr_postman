"""Seed fixtures into a test mailbox (Phase 1 helper).

APPENDs the sample .eml fixtures into the configured INBOX so the agent can be
exercised end-to-end against a real (test) mailbox. Use a THROWAWAY mailbox.

Usage:
    python -m scripts.seed_test_emails
    python -m scripts.seed_test_emails --only forwarded_replyto.eml
"""

from __future__ import annotations

import argparse
import imaplib
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent.config import load_scenario, resolve_secret

DEFAULT_SCENARIO = "config/scenarios/example.yaml"
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(description="Seed fixtures into the test mailbox")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    parser.add_argument("--only", default=None, help="single fixture filename")
    args = parser.parse_args(argv)

    config = load_scenario(args.scenario)
    imap_cfg = config.transport.imap
    password = resolve_secret(imap_cfg.password_env)

    files = (
        [FIXTURES / args.only]
        if args.only
        else sorted(FIXTURES.glob("*.eml"))
    )

    conn = (
        imaplib.IMAP4_SSL(imap_cfg.host, imap_cfg.port)
        if imap_cfg.ssl
        else imaplib.IMAP4(imap_cfg.host, imap_cfg.port)
    )
    conn.login(imap_cfg.username, password)
    for f in files:
        raw = f.read_bytes()
        conn.append(imap_cfg.inbox, "", imaplib.Time2Internaldate(time.time()), raw)
        print(f"appended {f.name} → {imap_cfg.inbox}")
    conn.logout()


if __name__ == "__main__":
    main()