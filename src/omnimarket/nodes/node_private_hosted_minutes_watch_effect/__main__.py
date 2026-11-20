# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one private-repository hosted Actions minutes watch for a complete UTC day."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError

from .container import container_private_hosted_minutes_watch_effect
from .models import ModelHostedMinutesWatchRequest
from .private_scope import format_minutes


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onex-private-hosted-minutes-watch")
    parser.add_argument("--omni-home", default=os.environ.get("OMNI_HOME"))
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".local/state/omni/private-hosted-minutes-watch",
    )
    parser.add_argument("--ledger", type=Path, default=None)
    parser.add_argument("--org", default="OmniNode-ai")
    parser.add_argument(
        "--day",
        type=date.fromisoformat,
        default=datetime.now(UTC).date() - timedelta(days=1),
        help="UTC day to check (YYYY-MM-DD); default: yesterday UTC",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = container_private_hosted_minutes_watch_effect.build_handler_private_hosted_minutes_watch().handle(
            ModelHostedMinutesWatchRequest(
                org=args.org,
                day=args.day,
                state_dir=args.state_dir,
                ledger=args.ledger,
            )
        )
    except NodeOverlayError as exc:
        sys.stderr.write(f"private-hosted-minutes-watch: overlay refused: {exc}\n")
        return 2
    except RuntimeError as exc:
        sys.stderr.write(f"private-hosted-minutes-watch: billing read failed: {exc}\n")
        return 2
    sys.stdout.write(
        f"private-hosted-minutes-watch: day={result.day} "
        f"hosted_minutes={format_minutes(result.total_day_minutes)} "
        f"repos={len(result.day_minutes)} alert={result.alert} "
        f"private_month_minutes={format_minutes(result.private_month_minutes)} "
        f"included={result.included_minutes} percent_used={format_minutes(result.percent_used)} "
        f"uncounted={format_minutes(sum(u.minutes for u in result.uncounted))} "
        f"ledger_written={result.ledger_written}\n"
    )
    for row in result.day_minutes:
        repository = (
            row.repository.replace("|", "/").replace("\r", " ").replace("\n", " ")
        )
        sys.stdout.write(f"  {repository}: {round(row.minutes, 2):g} min\n")
    return (
        0
        if result.ledger_written and result.alert in {"none", "sent", "already-sent"}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
