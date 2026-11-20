# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's commands on the overlay's backend (OMN-25981).

* ``plan``: fold the items and print the due owners as one JSON line (the Temporal workflow's first step).
* ``place``: read one due owner (JSON) on stdin, place its lane and write the owner on the item; print the result
  as one JSON line. The workflow runs it inside a child workflow whose id is the due owner's reservation key.
* ``tick``: ``plan`` then ``place`` for each due owner in one process, with an in-process reservation (by hand).
* ``read``: the unowned reading; exit 0 OK, 1 FINDING, 2 UNREAD; the STATUS row is appended by the node's identity.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.adapters.inmemory_adapters import (
    InMemoryBriefPreparer,
    InMemoryLanePlacer,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.adapters.runtime_adapters import (
    LaneBriefPreparer,
    SubprocessLanePlacer,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.adapters.inmemory_adapters import (
    InMemoryLedger,
    InMemoryPlanOwner,
    InMemoryReservation,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.adapters.runtime_adapters import (
    CommandPlanOwnerMissing,
    CommandRowAppender,
    LedgerQueryRows,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.handlers.handler_item_owner_dispatch import (
    HandlerItemOwnerDispatch,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.handlers.handler_unowned_reading import (
    EXIT_CODES,
    HandlerUnownedReading,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.models import (
    ModelItemOwnerRequest,
    ModelOwnerDue,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.overlay import (
    ModelItemOwnerOverlay,
    load_overlay,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.protocols import (
    LedgerUnreadableError,
)

REGISTRY_ROOT_TOKEN = "{registry_root}"
EXIT_PLACEMENT_FAILED = 1
EXIT_UNREADABLE = 3


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _resolve(argv: tuple[str, ...], root: Path) -> tuple[str, ...]:
    return tuple(part.replace(REGISTRY_ROOT_TOKEN, str(root)) for part in argv)


def _build_inmemory(
    overlay: ModelItemOwnerOverlay,
    *,
    root: Path,
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
) -> tuple[HandlerItemOwnerDispatch, HandlerUnownedReading]:
    dispatch = overlay.dispatch
    runner_path = environ.get(dispatch.runner_env, "")
    claude_dir = (
        environ.get(dispatch.claude_dir_env, "") if dispatch.claude_dir_env else ""
    )
    ledger = InMemoryLedger()
    plan_owner = InMemoryPlanOwner()
    briefs = InMemoryBriefPreparer()
    owner = HandlerItemOwnerDispatch(
        overlay=overlay,
        rows=ledger,
        plan_owner=plan_owner,
        reservation=InMemoryReservation(),
        briefs=briefs,
        runner=InMemoryLanePlacer(briefs),
        appender=ledger,
        runner_path=runner_path,
        claude_dir=claude_dir,
        clock=clock,
    )
    reading = HandlerUnownedReading(
        overlay=overlay,
        rows=ledger,
        plan_owner=plan_owner,
        appender=ledger,
        clock=clock,
    )
    return owner, reading


def _build_runtime(
    overlay: ModelItemOwnerOverlay,
    *,
    root: Path,
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
) -> tuple[HandlerItemOwnerDispatch, HandlerUnownedReading]:
    dispatch = overlay.dispatch
    runner_path = environ.get(dispatch.runner_env, "")
    claude_dir = (
        environ.get(dispatch.claude_dir_env, "") if dispatch.claude_dir_env else ""
    )
    if not runner_path:
        raise LedgerUnreadableError(
            f"{dispatch.runner_env} is not set: the unit's declaration names the remote-lane runner"
        )
    settings = overlay.ledger.model_copy(
        update={
            "query_command": _resolve(overlay.ledger.query_command, root),
            "append_command": _resolve(overlay.ledger.append_command, root),
        }
    )
    plan_settings = overlay.plan_owner.model_copy(
        update={"command": _resolve(overlay.plan_owner.command, root)}
    )
    rows = LedgerQueryRows(settings, clock)
    plan_owner_cmd = CommandPlanOwnerMissing(plan_settings)
    appender = CommandRowAppender(settings)
    briefs_dir = root / dispatch.briefs_dir
    plugin_root = (
        Path(runner_path).resolve().parents[overlay.brief.runner_root_levels - 1]
    )
    owner = HandlerItemOwnerDispatch(
        overlay=overlay,
        rows=rows,
        plan_owner=plan_owner_cmd,
        reservation=InMemoryReservation(),
        briefs=LaneBriefPreparer(
            briefs_dir,
            plugin_root,
            overlay.brief,
            model=dispatch.model,
            ticket=dispatch.ticket,
            environ=environ,
        ),
        runner=SubprocessLanePlacer(dispatch.settle_seconds, briefs_dir),
        appender=appender,
        runner_path=runner_path,
        claude_dir=claude_dir,
        clock=clock,
    )
    reading = HandlerUnownedReading(
        overlay=overlay,
        rows=rows,
        plan_owner=plan_owner_cmd,
        appender=appender,
        clock=clock,
    )
    return owner, reading


_BACKEND_BUILDERS = {
    "inmemory": _build_inmemory,
    "runtime": _build_runtime,
}


def build_handlers(
    overlay: ModelItemOwnerOverlay,
    *,
    root: Path,
    environ: Mapping[str, str],
    clock: Callable[[], datetime] = _utc_now,
) -> tuple[HandlerItemOwnerDispatch, HandlerUnownedReading]:
    """The owner step and the reading wired to the overlay's backend."""
    return _BACKEND_BUILDERS[overlay.backend](
        overlay, root=root, environ=environ, clock=clock
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "place", "tick", "read"))
    parser.add_argument("--omni-home", dest="registry_root", type=Path, required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    overlay = load_overlay()
    run_id = args.run_id or os.environ.get("ONEX_RUN_STEP_KEY", "") or "by-hand"
    try:
        owner, reading = build_handlers(
            overlay, root=args.registry_root, environ=os.environ
        )
        if args.command == "read":
            result = reading.read()
            sys.stdout.write(
                f"item-unowned verdict={result.verdict.value} unowned={len(result.unowned)} "
                f"items={result.items_read} appended={'yes' if result.status_row else 'no'} {result.detail}\n"
            )
            return EXIT_CODES[result.verdict]
        if args.command == "plan":
            sys.stdout.write(owner.plan().model_dump_json() + "\n")
            return 0
        if args.command == "place":
            due = ModelOwnerDue.model_validate_json(sys.stdin.read() or "{}")
            placed = owner.place(due, run_id, dry_run=args.dry_run)
            sys.stdout.write(placed.model_dump_json() + "\n")
            return 0
        result_tick = owner.handle(
            ModelItemOwnerRequest(run_id=run_id, dry_run=args.dry_run)
        )
    except LedgerUnreadableError as exc:
        sys.stdout.write(f"item-owner UNREADABLE {exc}\n")
        return EXIT_UNREADABLE
    started = sum(placed.started for placed in result_tick.placed)
    sys.stdout.write(
        f"item-owner run={run_id} items={len(result_tick.items)} placed={len(result_tick.placed)} "
        f"started={started} dry_run={str(args.dry_run).lower()}\n"
    )
    for placed in result_tick.placed:
        state = (
            "started"
            if placed.started
            else (placed.skip.value if placed.skip else "failed")
        )
        sys.stdout.write(
            f"OWNER {state} {placed.item_id} {placed.lane} {placed.detail}\n"
        )
    failed = [
        p
        for p in result_tick.placed
        if not p.started and p.skip is not None and p.skip.value != "dry-run"
    ]
    return EXIT_PLACEMENT_FAILED if failed else 0


if __name__ == "__main__":
    sys.exit(main())
