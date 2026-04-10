# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_environment_health_scanner.

Usage:
    python -m omnimarket.nodes.node_environment_health_scanner --all
    python -m omnimarket.nodes.node_environment_health_scanner --subsystem kafka
    python -m omnimarket.nodes.node_environment_health_scanner --subsystem hooks
    python -m omnimarket.nodes.node_environment_health_scanner --subsystem emit_daemon,kafka,containers

Output: JSON to stdout (EnvironmentHealthResult model) with --json flag, else human-readable.
Exit codes: 0=PASS, 1=FAIL, 2=WARN
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from omnimarket.nodes.node_environment_health_scanner.handlers.handler_environment_health_scanner import (
    EnumSubsystem,
    EnvironmentHealthRequest,
    EnvironmentHealthResult,
    NodeEnvironmentHealthScanner,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)

_log = logging.getLogger(__name__)

_ALL_SUBSYSTEMS = [s.value for s in EnumSubsystem]


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="ONEX live environment health scanner — contract-driven environment verification"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all",
        action="store_true",
        help="Run all 7 subsystem probers",
    )
    group.add_argument(
        "--subsystem",
        metavar="SUBSYSTEM[,SUBSYSTEM...]",
        help=f"Comma-separated subsystems to scan. Options: {', '.join(_ALL_SUBSYSTEMS)}",
    )
    parser.add_argument(
        "--ssh-target",
        default=os.environ.get("ONEX_INFRA_SSH_TARGET"),
        help="SSH target for .201 infra probes (default: $ONEX_INFRA_SSH_TARGET)",
    )
    parser.add_argument(
        "--omni-home",
        default=os.environ.get("OMNI_HOME", ""),
        help="Path to omni_home (default: $OMNI_HOME)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output full JSON (default: human-readable summary)",
    )
    args = parser.parse_args()

    subsystems = (
        _ALL_SUBSYSTEMS if args.all else [s.strip() for s in args.subsystem.split(",")]
    )

    unknown = [s for s in subsystems if s not in _ALL_SUBSYSTEMS]
    if unknown:
        parser.error(
            f"Unknown subsystem(s): {', '.join(unknown)}. Valid: {', '.join(_ALL_SUBSYSTEMS)}"
        )

    request = EnvironmentHealthRequest(
        subsystems=subsystems,
        omni_home=args.omni_home,
        ssh_target=args.ssh_target,
    )

    handler = NodeEnvironmentHealthScanner()
    result = handler.handle(request)

    if args.json:
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    else:
        _print_human_report(result)

    exit_code = {
        EnumReadinessStatus.PASS: 0,
        EnumReadinessStatus.WARN: 2,
        EnumReadinessStatus.FAIL: 1,
    }.get(result.overall, 1)
    sys.exit(exit_code)


def _print_human_report(result: EnvironmentHealthResult) -> None:
    print("\n=== ONEX Environment Health Scan ===")  # noqa: T201
    print(f"Overall: {result.overall}")  # noqa: T201
    print(  # noqa: T201
        f"  PASS: {result.pass_count}  WARN: {result.warn_count}  FAIL: {result.fail_count}\n"
    )
    for sub in result.subsystem_results:
        icon = {"PASS": "v", "WARN": "!", "FAIL": "x"}.get(sub.status.value, "?")
        print(  # noqa: T201
            f"  [{icon}] {sub.subsystem.value:<20} {sub.status.value}  ({sub.check_count} checks)"
        )
        for f in sub.findings:
            print(f"       {f.severity}: {f.subject} -- {f.message}")  # noqa: T201
    print()  # noqa: T201


if __name__ == "__main__":
    main()
