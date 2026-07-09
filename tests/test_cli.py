"""CLI arg parsing — `--scenario` must work before AND after the subcommand.

The docs and the generated systemd unit (`run_agent run --scenario …`) put the
flag after the subcommand; plain argparse subparsers reject that. These lock in
the position-independent behavior so a bad flag order never crash-loops the unit.
"""

from __future__ import annotations

from scripts.run_agent import DEFAULT_SCENARIO, build_parser


def test_scenario_before_subcommand():
    args = build_parser().parse_args(["--scenario", "x.yaml", "once"])
    assert args.cmd == "once" and args.scenario == "x.yaml"


def test_scenario_after_subcommand():
    # This is the form the systemd unit + docs use — previously an argparse error.
    args = build_parser().parse_args(["run", "--scenario", "x.yaml"])
    assert args.cmd == "run" and args.scenario == "x.yaml"


def test_scenario_defaults_when_absent():
    args = build_parser().parse_args(["once"])
    assert args.scenario == DEFAULT_SCENARIO


def test_scenario_after_subcommand_with_positional():
    args = build_parser().parse_args(["approve", "abc123", "--scenario", "y.yaml"])
    assert args.cmd == "approve"
    assert args.approval_id == "abc123"
    assert args.scenario == "y.yaml"