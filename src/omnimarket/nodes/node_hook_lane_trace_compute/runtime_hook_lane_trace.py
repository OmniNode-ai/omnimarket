# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_hook_lane_trace_compute --lane <lane> [--since S] [--until U] [--hours N] [--limit N]``.

Without ``--rows`` it prints the READ ONLY query that reads the lane's completed calls with their stored output. With
``--rows -`` it reads, from standard input, the lines that query returned (``tc|<json>``; any other line is skipped) and prints each
call with its label, exit code, lane and full output, ending in one outcomes line::

    outcomes calls=N ok=A failed=B refused=C timeout=D no-output=E

Exit codes: 0 answered; 2 the request is refused (a lane name that is not a plain token, a bound that is not a UTC
stamp, a limit under 1) or a rows line is bad; 3 the outcome overlay is absent or invalid (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_hook_lane_trace_compute.handlers.handler_hook_lane_trace import (
    HandlerHookLaneTrace,
    build_sql,
)
from omnimarket.nodes.node_hook_lane_trace_compute.models import (
    ModelLaneTraceRequest,
    ModelLaneTraceRow,
)
from omnimarket.nodes.node_hook_lane_trace_compute.models.model_lane_trace_request import (
    STAMP_RE,
    TRACE_LIMIT,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.handlers.handler_hook_tool_outcome import (
    HandlerHookToolOutcome,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.overlay import load_outcome_rules

ROWS_PREFIX = "tc|"


def _stamp(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_rows(stdin_or_file: Any) -> tuple[ModelLaneTraceRow, ...]:
    rows: list[ModelLaneTraceRow] = []
    for number, line in enumerate(stdin_or_file.read().splitlines(), start=1):
        if not line.startswith(ROWS_PREFIX):
            continue
        try:
            rows.append(
                ModelLaneTraceRow.model_validate(json.loads(line[len(ROWS_PREFIX) :]))
            )
        except (ValueError, ValidationError) as exc:
            raise ValueError(f"rows line {number}: {exc}") from None
    return tuple(rows)


def _run(
    argv: Sequence[str] | None,
    stdin: Any,
    stdout: Any,
    stderr: Any,
    environ: Mapping[str, str] | None,
    now: datetime | None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="node_hook_lane_trace_compute", description=__doc__
    )
    parser.add_argument("--lane", required=True, help="the lane name, a plain token")
    parser.add_argument(
        "--since", help="start bound, a UTC stamp; default --hours before --until"
    )
    parser.add_argument("--until", help="end bound, a UTC stamp; default now")
    parser.add_argument(
        "--hours",
        type=float,
        default=6.0,
        help="window length when --since is not given",
    )
    parser.add_argument(
        "--limit", type=int, default=TRACE_LIMIT, help="newest calls the query reads"
    )
    parser.add_argument(
        "--rows",
        help="a file of the query's output lines, or - for stdin; omit to print the query",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    until = args.until if args.until is not None else _stamp(now or datetime.now(UTC))
    if args.since is not None:
        since = args.since
    elif STAMP_RE.fullmatch(until):
        since = _stamp(
            datetime.fromisoformat(until.replace("Z", "+00:00"))
            - timedelta(hours=args.hours)
        )
    else:
        since = until  # the request refuses the end bound by name
    try:
        if args.rows not in (None, "-"):
            raise ValueError(
                "--rows reads standard input only: pass - and pipe the query's output in"
            )
        rows = _read_rows(stdin) if args.rows == "-" else None
        request = ModelLaneTraceRequest(
            lane=args.lane, since=since, until=until, limit=args.limit, rows=rows
        )
    except (ValidationError, ValueError) as exc:
        stderr.write(f"lane-trace refused: {exc}\n")
        return 2

    if rows is None:
        stdout.write(build_sql(request))
        return 0
    try:
        handler = HandlerHookLaneTrace(
            HandlerHookToolOutcome(load_outcome_rules(environ))
        )
    except (NodeOverlayError, ValidationError) as exc:
        stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    for line in handler.handle(request).lines:
        stdout.write(line + "\n")
    return 0


def main(
    argv: Sequence[str] | None = None,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    return _run(
        argv,
        stdin or sys.stdin,
        stdout or sys.stdout,
        stderr or sys.stderr,
        environ,
        now,
    )


if __name__ == "__main__":
    raise SystemExit(main())
