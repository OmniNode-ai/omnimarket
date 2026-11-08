# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_commitment_check_runner_effect``: run every due commitment's check once.

What the schedule's unit runs. The ledger routes, the allowlist and every bound are the overlay's
(``ONEX_COMMITMENT_CHECK_OVERLAY`` pins the file, else ``node_commitment_check_runner_effect/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``); the commitment row rules are node_commitment_ledger_compute's overlay.

Exit codes: 0 the run completed (a check that failed is still 0: it is a reading, not a fault of the run); 1 the run failed
(the ledger could not be read) or a reading was not accepted by the ledger; 3 a deployment fact is missing (an overlay or a
route variable).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.commitment import (
    ModelCommitmentRules,
)
from omnimarket.nodes.node_commitment_check_runner_effect.adapters.executors import (
    SubprocessCheckExecutor,
)
from omnimarket.nodes.node_commitment_check_runner_effect.handlers.handler_commitment_check_runner import (
    HandlerCommitmentCheckRunner,
)
from omnimarket.nodes.node_commitment_check_runner_effect.handlers.schedule import (
    CommitmentCheckScheduler,
    load_schedule,
)
from omnimarket.nodes.node_commitment_check_runner_effect.models import (
    ModelCommitmentCheckCommand,
    ModelCommitmentCheckRules,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
    build_row_source,
)

NODE = "node_commitment_check_runner_effect"
LEDGER_NODE = "node_commitment_ledger_compute"
OVERLAY_ENV = "ONEX_COMMITMENT_CHECK_OVERLAY"
LEDGER_OVERLAY_ENV = "ONEX_COMMITMENT_OVERLAY"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"


def _schedule(arguments: Sequence[str]) -> int:
    """``schedule install|uninstall|status|run-now --host <name>``: the timer on a declared lab host."""
    parser = argparse.ArgumentParser(prog="schedule")
    verbs = parser.add_subparsers(dest="verb", required=True)
    for verb in ("install", "uninstall", "status", "run-now"):
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True, help="the host's name in the overlay")
        if verb == "install":
            sub.add_argument(
                "--workspace-root",
                dest="workspace_root",
                default=os.environ.get("OMNI_HOME", ""),
            )
            sub.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(arguments))
    try:
        rules = load_structured_overlay(
            NODE, ModelCommitmentCheckRules, file_env=OVERLAY_ENV
        )
        scheduler = CommitmentCheckScheduler(
            schedule=load_schedule(), overlay=rules, home=Path.home()
        )
        if args.verb == "install":
            if not args.workspace_root:
                raise ValueError(
                    "OMNI_HOME is not set and --workspace-root was not passed"
                )
            # The unit carries the overlay configuration this install validated: every root and each pinned file.
            roots = [
                Path(r).expanduser().resolve()
                for r in os.environ.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep)
                if r
            ]
            pins = {
                name: Path(os.environ[name]).expanduser().resolve()
                for name in (OVERLAY_ENV, LEDGER_OVERLAY_ENV)
                if os.environ.get(name, "").strip()
            }
            result = scheduler.install(
                args.host,
                omni_root=Path(args.workspace_root).expanduser().resolve(),
                overlay_roots=roots,
                overlay_pins=pins,
                dry_run=args.dry_run,
            )
        elif args.verb == "uninstall":
            result = scheduler.uninstall(args.host)
        elif args.verb == "status":
            result = scheduler.status(args.host)
        else:
            result = scheduler.run_now(args.host)
    except (NodeOverlayError, ValueError, OSError) as exc:
        sys.stderr.write(f"commitment-check: refused ({type(exc).__name__}: {exc})\n")
        return 3
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    given = list(sys.argv[1:] if argv is None else argv)
    if given and given[0] == "schedule":
        return _schedule(given[1:])
    parser = argparse.ArgumentParser(prog=f"python -m omnimarket.nodes.{NODE}")
    parser.add_argument(
        "--correlation-id", default=None, help="defaults to a fresh id for this run"
    )
    arguments = parser.parse_args(given)
    try:
        rules = load_structured_overlay(
            NODE, ModelCommitmentCheckRules, file_env=OVERLAY_ENV
        )
        commitment_rules = load_structured_overlay(
            LEDGER_NODE, ModelCommitmentRules, file_env=LEDGER_OVERLAY_ENV
        )
        source = build_row_source(rules.ledger_read)
        appender = build_appender(rules.ledger_append, rules.runner_lane)
    except (NodeOverlayError, LedgerRouteError) as exc:
        sys.stderr.write(f"commitment-check: refused ({type(exc).__name__}: {exc})\n")
        return 3
    handler = HandlerCommitmentCheckRunner(
        rules=rules,
        commitment_rules=commitment_rules,
        source=source,
        appender=appender,
        executor=SubprocessCheckExecutor(rules.output_tail_chars),
        clock=lambda: datetime.now(UTC),
        run_budget_seconds=load_schedule().timeout_seconds,
    )
    result = handler.handle(
        ModelCommitmentCheckCommand(
            correlation_id=arguments.correlation_id or uuid.uuid4().hex
        )
    )
    if result.status == "failed":
        sys.stdout.write(f"commitment-check: FAILED {result.failure}\n")
        return 1
    lines = [
        f"{o.commitment_id} {o.status_before.value}->{o.status_after.value} "
        f"{o.result} exit={o.exit_code}"
        for o in result.outcomes
    ]
    lines.append(
        f"commitment-check: checked={sum(1 for o in result.outcomes if o.ran)} "
        f"refused={sum(1 for o in result.outcomes if not o.ran)} malformed={len(result.malformed)} "
        f"deferred={result.deferred}"
    )
    sys.stdout.write("\n".join(lines) + "\n")
    return (
        1 if any(o.result.startswith("append-failed:") for o in result.outcomes) else 0
    )


__all__ = ["main"]
