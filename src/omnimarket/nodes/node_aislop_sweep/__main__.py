# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_aislop_sweep.

Usage:
    python -m omnimarket.nodes.node_aislop_sweep \
        --repos omniclaude,omnibase_core \
        --checks prohibited-patterns,hardcoded-topics \
        --dry-run

Outputs JSON to stdout: AislopSweepResult model.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import cast

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.cli.args import (
    add_output_args,
    report_output_requested,
    resolve_output_config,
)
from omnimarket.cli.output.registry import resolve_handler
from omnimarket.cli.reporting import (
    build_report_from_model_result,
    load_contract_metadata,
)
from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
    AislopSweepRequest,
    NodeAislopSweep,
)

_log = logging.getLogger(__name__)

_DEFAULT_REPOS = [
    "omniclaude",
    "omnibase_core",
    "omnibase_infra",
    "omnibase_spi",
    "omniintelligence",
    "omnimemory",
    "onex_change_control",
    "omnibase_compat",
]


def _resolve_repo_dirs(repos: list[str], omni_home: str) -> list[str]:
    """Resolve repo names to absolute paths under omni_home."""
    root = Path(omni_home)
    resolved: list[str] = []
    for repo in repos:
        p = root / repo
        if p.is_dir():
            resolved.append(str(p))
        else:
            _log.warning("repo dir not found: %s", p)
    return resolved


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    omni_home = os.environ.get("OMNI_HOME")
    if not omni_home:
        _log.error("OMNI_HOME is not set — cannot resolve repo directories")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Detect AI-generated quality anti-patterns across repos."
    )
    parser.add_argument(
        "--repos",
        default="",
        help="Comma-separated repo names (default: all supported repos)",
    )
    parser.add_argument(
        "--checks",
        default="",
        help=(
            "Comma-separated check categories: "
            "phantom-callables,compat-shims,prohibited-patterns,"
            "hardcoded-topics,todo-fixme,todo-stale,empty-impls "
            "(default: all)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Scan and report only — no tickets, no fixes",
    )
    parser.add_argument(
        "--severity-threshold",
        default="WARNING",
        choices=["WARNING", "ERROR", "CRITICAL"],
        help="Minimum severity to act on (default: WARNING)",
    )
    add_output_args(parser)

    args = parser.parse_args()
    output_config = resolve_output_config(args)
    logging.getLogger().setLevel(args.log_level.upper())

    repos = [r.strip() for r in args.repos.split(",") if r.strip()] or _DEFAULT_REPOS
    checks = [c.strip() for c in args.checks.split(",") if c.strip()] or None

    target_dirs = _resolve_repo_dirs(repos, omni_home)
    if not target_dirs:
        _log.error("no valid repo directories resolved")
        sys.exit(1)

    request = AislopSweepRequest(
        target_dirs=target_dirs,
        checks=checks,
        dry_run=args.dry_run,
        severity_threshold=args.severity_threshold,
    )

    handler = NodeAislopSweep(
        event_bus=cast(ProtocolEventBusPublisher, EventBusInmemory())
    )
    result = handler.handle(request)
    if not report_output_requested():
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
        if result.status not in ("clean",):
            sys.exit(1)
        return

    contract_name, contract_version, terminal_event = load_contract_metadata(
        "omnimarket.nodes.node_aislop_sweep"
    )
    cli_report = build_report_from_model_result(
        result,
        skill_name="aislop_sweep",
        node_name="node_aislop_sweep",
        terminal_event=terminal_event,
        contract_name=contract_name,
        contract_version=contract_version,
        mode="dry_run" if args.dry_run else "execute",
        input_summary={
            "repos": repos,
            "checks": checks,
            "dry_run": args.dry_run,
            "severity_threshold": args.severity_threshold,
        },
        output_config=output_config,
    )
    rendered = resolve_handler(output_config.format).render(cli_report)
    sys.stdout.write(rendered)
    if not rendered.endswith("\n"):
        sys.stdout.write("\n")

    # Exit non-zero when findings exist
    if result.status not in ("clean",):
        sys.exit(1)


if __name__ == "__main__":
    main()
