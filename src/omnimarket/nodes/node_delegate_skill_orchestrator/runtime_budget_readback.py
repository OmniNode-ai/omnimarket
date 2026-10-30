# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21326 AC3: no completed deployed-lane run outlives its budget plus grace.

Command line entry of the delegation node's receipt-window readback. It parses
the arguments, runs ``handler_budget_readback`` over the selected state roots,
prints the summary as one JSON line and exits with the verdict:

    0  at least one selected run, none over
    1  one or more selected runs over the bound
    2  no selected run in the window: nothing was measured
    3  the check could not run (no state root, or the declaration is ambiguous)

    python -m omnimarket.nodes.node_delegate_skill_orchestrator.runtime_budget_readback
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_budget_readback import (
    EXIT_UNRUNNABLE,
    CheckUnrunnableError,
    declared_grace_seconds,
    default_state_roots,
    measure,
    verdict,
)


def _utc(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--state-root",
        action="append",
        type=Path,
        help=(
            "onex delegate state root; repeatable "
            "(default: ONEX_STATE_DIR, else ONEX_STATE_ROOT, else ~/.onex_state)"
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
    roots = args.state_root or default_state_roots()
    try:
        missing = [str(root) for root in roots if not root.is_dir()]
        if missing:
            raise CheckUnrunnableError(f"state root not found: {', '.join(missing)}")
        summary = measure(roots, since, until, declared_grace_seconds())
    except CheckUnrunnableError as error:
        sys.stdout.write(json.dumps({"error": str(error)}) + "\n")
        return EXIT_UNRUNNABLE
    sys.stdout.write(json.dumps(summary) + "\n")
    return verdict(summary)


if __name__ == "__main__":
    sys.exit(main())
