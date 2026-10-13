# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI adapter: every reconciliation command travels through the contract's bus (OMN-20677).

``python -m omnimarket.nodes.node_ledger_reconcile_orchestrator`` keeps the exits of the
reconciler it replaces: 0 clean, 1 dangling claims or stale holds reported or
reconciled, 2 an append failed, 3 not run, 4 refused by the append cap.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from omnibase_core.runtime.runtime_local import RuntimeLocal

from omnimarket.models.ledger_reconcile import (
    ModelReconcileRequest,
    ModelReconcileResult,
)


def parse_request(
    argv: list[str] | None = None,
) -> tuple[ModelReconcileRequest, bool, bool]:
    parser = argparse.ArgumentParser(
        description="Mechanical dangling-claim reconciliation"
    )
    parser.add_argument("--stale-hours", type=float, default=6.0)
    parser.add_argument("--since-days", type=float, default=7.0, help="0 = all history")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--max-appends", type=int, default=None)
    parser.add_argument("--live-lanes", type=Path, default=None)
    parser.add_argument("--live-lane", action="append", default=[])
    parser.add_argument(
        "--live-roster",
        action="store_true",
        help="the --live-lane values are the complete current roster, even when none are given",
    )
    parser.add_argument(
        "--silent-hours",
        type=float,
        default=None,
        help="retire handle-free, inactive claims as abandoned after this silence bound; "
        "requires a live roster (never asserts completion)",
    )
    parser.add_argument(
        "--workflow", action="store_true", help="successful reconciliation exits zero"
    )
    parser.add_argument(
        "--json", action="store_true", help="print the typed terminal event"
    )
    args = parser.parse_args(argv)
    return (
        ModelReconcileRequest(
            stale_hours=args.stale_hours,
            since_days=args.since_days,
            apply=args.apply,
            max_appends=args.max_appends,
            live_lanes_file=args.live_lanes,
            live_lanes=frozenset(args.live_lane),
            live_roster_known=args.live_roster,
            silent_hours=args.silent_hours,
        ),
        args.json,
        args.workflow,
    )


def main(argv: list[str] | None = None) -> int:
    request, as_json, workflow = parse_request(argv)
    # The ledger and local git evidence are host-bound operator resources. Host the
    # handler on this process's bus; never invoke it directly or select a fallback.
    with tempfile.TemporaryDirectory(prefix="onex-ledger-reconcile-") as directory:
        root = Path(directory)
        payload = root / "request.json"
        payload.write_text(request.model_dump_json(), encoding="utf-8")
        runtime = RuntimeLocal(
            Path(__file__).parent / "contract.yaml",
            state_root=root / "state",
            input_path=payload,
            backend_overrides={"event_bus": "inmemory"},
        )
        runtime.run()
        result = runtime.handler_result
        if not isinstance(result, ModelReconcileResult):
            sys.stderr.write(
                f"ledger_reconcile: NOT RUN — {runtime.last_error or 'no terminal event'}\n"
            )
            return 3
        if as_json:
            sys.stdout.write(result.model_dump_json() + "\n")
        else:
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
        return 0 if workflow and result.exit_code in (0, 1) else result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
