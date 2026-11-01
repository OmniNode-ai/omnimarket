# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21326 AC3: no completed deployed-lane run outlives its budget plus grace.

Command line entry of the delegation node's receipt-window readback. It parses
the arguments, runs ``handler_budget_readback`` over the lab projection (or,
when ``--state-root`` or ``--source state-root`` is given, over local ``onex
delegate`` state roots), prints the summary as one JSON line and exits with the
verdict:

    0  at least one selected run, none over
    1  one or more selected runs over the bound
    2  no selected run in the window: nothing was measured
    3  the check could not run (source unreadable, or the declaration is ambiguous)

The projection is the default, so the command runs from any lab host that has
the contract-declared DSN variable set (``budget_readback.source.dsn_env`` in
this node's contract.yaml), with no access to the machine that ran the CLI.

    python -m omnimarket.nodes.node_delegate_skill_orchestrator.runtime_budget_readback
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.budget_reading_source import (
    BudgetSourceUnreadableError,
    ProjectionBudgetReadingSource,
    ProtocolBudgetReadingSource,
    StateRootBudgetReadingSource,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_budget_readback import (
    EXIT_UNRUNNABLE,
    CheckUnrunnableError,
    declared_grace_seconds,
    default_state_roots,
    measure,
    verdict,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_budget_readback_source import (
    load_budget_readback_source,
)

SOURCE_PROJECTION = "projection"
SOURCE_STATE_ROOT = "state-root"


def _utc(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main(
    argv: Sequence[str] | None = None,
    *,
    projection: ProtocolBudgetReadingSource | None = None,
) -> int:
    """Run the readback; ``projection`` replaces the database source in tests."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        choices=(SOURCE_PROJECTION, SOURCE_STATE_ROOT),
        help=(
            "where finished runs are read from (default: the lab projection; "
            "--state-root implies state-root)"
        ),
    )
    parser.add_argument(
        "--state-root",
        action="append",
        type=Path,
        help=(
            "local onex delegate state root; repeatable "
            "(default for --source state-root: ONEX_STATE_DIR, else "
            "ONEX_STATE_ROOT, else ~/.onex_state)"
        ),
    )
    parser.add_argument(
        "--since",
        type=_utc,
        help="window start, ISO 8601 (default: --hours before --until)",
    )
    parser.add_argument(
        "--until", type=_utc, help="window end, ISO 8601 (default: now)"
    )
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args(argv)

    until = args.until or datetime.now(tz=UTC)
    since = args.since or until - timedelta(hours=args.hours)
    use_state_root = args.source == SOURCE_STATE_ROOT or (
        args.source is None and bool(args.state_root)
    )
    if args.source == SOURCE_PROJECTION and args.state_root:
        parser.error("--state-root cannot be combined with --source projection")
    try:
        source: ProtocolBudgetReadingSource
        if use_state_root:
            source = StateRootBudgetReadingSource(
                args.state_root or default_state_roots()
            )
        elif projection is not None:
            source = projection
        else:
            source = ProjectionBudgetReadingSource(load_budget_readback_source())
        readings = source.read(since, until)
        summary = measure(
            readings, since, until, declared_grace_seconds(), source.describe()
        )
    except (CheckUnrunnableError, BudgetSourceUnreadableError) as error:
        sys.stdout.write(json.dumps({"error": str(error)}) + "\n")
        return EXIT_UNRUNNABLE
    sys.stdout.write(json.dumps(summary) + "\n")
    return verdict(summary)


if __name__ == "__main__":
    sys.exit(main())
