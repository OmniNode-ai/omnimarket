# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_validator_estate_reading_effect <verb>``.

``read``   read the estate and append the row.
``probe``  the same read, appending nothing.
``schedule`` check | install | uninstall | status | run-now, for a host in the overlay (the daily timer runs ``read``).

``--fault core-unreadable`` injects the declared fault; the row then carries ``drill=yes`` and no numbers.

Exit codes: 0 the read ran (a low share is a reading, not a failure); 1 the run failed (an append that did
not land); 3 a deployment fact is missing.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError, find_node_overlay
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)
from omnimarket.nodes.node_validator_estate_reading_effect.adapters.git_estate_source import (
    GitEstateSource,
)
from omnimarket.nodes.node_validator_estate_reading_effect.handlers.handler_validator_estate import (
    HandlerValidatorEstate,
)
from omnimarket.nodes.node_validator_estate_reading_effect.handlers.schedule import (
    EstateScheduler,
    load_schedule,
)
from omnimarket.nodes.node_validator_estate_reading_effect.models.model_validator_estate import (
    ModelValidatorEstateCommand,
)
from omnimarket.nodes.node_validator_estate_reading_effect.overlay import (
    NODE_NAME,
    load_overlay,
)


def build_handler(write: bool) -> HandlerValidatorEstate:
    overlay = load_overlay()
    root = os.environ.get(overlay.repos_root_env, "").strip()
    host = os.environ.get(overlay.ledger_append.host_env, "").strip()
    if not root:
        raise ValueError(
            f"{overlay.repos_root_env} is not set; the overlay names it as the clones' root"
        )
    if not host:
        raise ValueError(
            f"{overlay.ledger_append.host_env} is not set; the overlay names it as this host"
        )
    plan_root = os.environ.get(overlay.plan.root_env, "").strip()
    if not plan_root:
        raise ValueError(
            f"{overlay.plan.root_env} is not set; the overlay names it as the plan clone's root"
        )
    return HandlerValidatorEstate(
        overlay=overlay,
        host=host,
        source=GitEstateSource(Path(root), overlay.git_timeout_seconds),
        plan_source=GitEstateSource(Path(plan_root), overlay.git_timeout_seconds),
        appender=build_appender(overlay.ledger_append, overlay.publish.lane)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
    )


def _schedule(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="schedule")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("check")
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
    scheduler = EstateScheduler(
        schedule=load_schedule(), overlay=load_overlay(), home=Path.home()
    )
    if args.verb == "check":
        result = scheduler.check()
    elif args.verb == "install":
        if not args.workspace_root:
            raise ValueError("OMNI_HOME is not set and --workspace-root was not passed")
        found = find_node_overlay(NODE_NAME)
        if found is None:
            raise NodeOverlayError(
                "the overlay file cannot be found to pin its root in the unit"
            )
        result = scheduler.install(
            args.host,
            omni_root=Path(args.workspace_root),
            overlay_root=found.parent.parent,
            dry_run=args.dry_run,
        )
    elif args.verb == "uninstall":
        result = scheduler.uninstall(args.host)
    elif args.verb == "status":
        result = scheduler.status(args.host)
    else:
        result = scheduler.run_now(args.host)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    given = list(sys.argv[1:] if argv is None else argv)
    if given and given[0] == "schedule":
        try:
            return _schedule(given[1:])
        except (NodeOverlayError, LedgerRouteError, ValueError) as exc:
            sys.stderr.write(f"INDETERMINATE {exc}\n")
            return 3
    parser = argparse.ArgumentParser(prog="node_validator_estate_reading_effect")
    parser.add_argument("verb", choices=("read", "probe"))
    parser.add_argument("--fault", choices=("none", "core-unreadable"), default="none")
    arguments = parser.parse_args(given)
    write = arguments.verb == "read"
    try:
        handler = build_handler(write)
    except (ValueError, LedgerRouteError) as exc:
        sys.stderr.write(f"INDETERMINATE {exc}\n")
        return 3
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelValidatorEstateCommand(
            correlation_id=f"validator-estate-{stamp}",
            write=write,
            fault=arguments.fault,
        )
    )
    sys.stdout.write(
        f"RUN outcome={result.outcome} status={result.status} ledger={result.ledger}"
        f" canonical={result.canonical}/{result.estate} other_hooks={result.other_hooks}"
        f" other_classes={result.other_classes} traces={result.canonical_emitting}/{result.canonical}"
        f" tasks_merged={result.tasks_merged}/{len(result.tasks)}\n"
    )
    for c in result.classes:
        sys.stdout.write(
            f"CLASS {c.repo} {c.module} canonical={c.canonical} data={c.rules_as_data}"
            f" literals={c.literal_tables} traces={c.emits_trace}\n"
        )
    for h in result.hooks:
        sys.stdout.write(
            f"HOOKS {h.repo} total={h.hooks} covered={h.covered} pytest={h.pytest}\n"
        )
    for t in result.tasks:
        sys.stdout.write(f"TASK {t.task} {t.state} tickets={','.join(t.tickets)}\n")
    for item in result.unreadable:
        sys.stdout.write(f"UNREADABLE {item}\n")
    if result.row:
        sys.stdout.write(f"ROW {result.row}\n")
    return 1 if result.status == "failed" else 0
