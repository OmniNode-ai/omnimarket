# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the dropped-work audit once from a shell: ``python -m omnimarket.nodes.node_dropped_work_audit_scheduled_effect.runtime_dropped_work_audit --dry-run``.

A dry run reads the ledger, the pull request state and the admission refusals through the overlays and prints the count of
items per kind, the number with no live owner, the events it would publish and the rows it would append. Nothing is
published or appended. Without ``--dry-run`` the run does what the runtime tick does.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from omnimarket.handlers.dropped_work_audit_scheduled_tick import (
    DroppedWorkAuditSourceError,
    run_dropped_work_audit_tick,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.dropped_work_audit import ModelDroppedWorkAuditScheduleOverlay
from omnimarket.nodes.node_dropped_work_audit_scheduled_effect.adapters.publisher import (
    EmitOwnerMissingPublisher,
)

NODE_NAME = "node_dropped_work_audit_scheduled_effect"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--rows", type=int, default=3, help="how many alarm rows to print"
    )
    parser.add_argument(
        "--now", help="the audit's clock, an ISO time (default: the system clock)"
    )
    args = parser.parse_args(argv)
    fixed = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00")).astimezone(UTC)
        if args.now
        else None
    )
    try:
        schedule = load_structured_overlay(
            NODE_NAME, ModelDroppedWorkAuditScheduleOverlay, None
        )
        outcome = run_dropped_work_audit_tick(
            schedule=schedule,
            environ=None,
            clock=(lambda: fixed) if fixed is not None else (lambda: datetime.now(UTC)),
            evented=set(),
            publisher=None if args.dry_run else EmitOwnerMissingPublisher(),
            dry_run=args.dry_run,
        )
    except (NodeOverlayError, DroppedWorkAuditSourceError) as exc:
        sys.stderr.write(f"{NODE_NAME}: {exc}" + "\n")
        return 2
    result = outcome.result
    if result is not None:
        sys.stdout.write(f"now={result.now:%Y-%m-%dT%H:%M:%SZ}" + "\n")
        for kind, count in sorted(result.counts_by_kind.items()):
            unowned = sum(
                1 for i in result.items if i.kind.value == kind and not i.owned
            )
            sys.stdout.write(f"kind={kind} items={count} unowned={unowned}" + "\n")
        sys.stdout.write(f"items={len(result.items)} unowned={result.unowned}" + "\n")
        sys.stdout.write(f"events_would_publish={len(result.events)}" + "\n")
        sys.stdout.write(f"alarm_rows_would_append={len(result.alarms)}" + "\n")
        for alarm in result.alarms[: max(args.rows, 0)]:
            sys.stdout.write(f"ROW {alarm.row}" + "\n")
    sys.stdout.write(outcome.detail + "\n")
    return 1 if outcome.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
