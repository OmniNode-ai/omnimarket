# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A reader's window of ledger rows from the bus-fed rows loader node, never from the ledger file.

``node_ledger_rows_loader_effect`` answers a window of rows from ``node_work_ledger_reader_effect``'s
``rows`` read. This module runs that node's entrypoint for a caller that has no ledger file to name
and returns the rows it printed. A node that does not answer (exit 69, a refused read or a node that
did not run) is ``LedgerRowsUnreadableError``: an unread window is never an empty one, and the file is
never read in its place.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from omnimarket.handlers.ledger_rows_unreadable_error import LedgerRowsUnreadableError

ROWS_LOADER_MODULE = "omnimarket.nodes.node_ledger_rows_loader_effect"
#: The loader refuses a window over 30 days.
MAX_WINDOW_HOURS = 720
DEFAULT_TIMEOUT_S = 300.0

ProtocolRunner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


def window_hours_covering(start: datetime, now: datetime, margin_hours: int = 1) -> int:
    """Whole hours from ``start`` to ``now`` plus a margin, held to the loader's bounds."""
    span = now - start
    hours = int(span / timedelta(hours=1)) + 1 + margin_hours
    return max(1, min(MAX_WINDOW_HOURS, hours))


def _default_runner(
    argv: list[str], timeout_s: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout_s, check=False
    )


def read_ledger_rows_range_via_node(
    reader: str,
    since: datetime,
    until: datetime | None = None,
    *,
    kinds: tuple[str, ...] = (),
    runner: ProtocolRunner = _default_runner,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[str, ...]:
    """The rows from ``since`` to ``until`` (inclusive; open means now) at any age the store holds.

    The history read: a range wider than the store holds is a refused loader run, so it is
    :class:`LedgerRowsUnreadableError` like any unanswered read, never a short answer.
    """
    argv = [sys.executable, "-m", ROWS_LOADER_MODULE, "--reader", reader]
    argv += ["--since", _stamp(since)]
    if until is not None:
        argv += ["--until", _stamp(until)]
    for kind in kinds:
        argv += ["--kind", kind]
    return _run(argv, reader, runner, timeout_s)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_ledger_rows_via_node(
    reader: str,
    window_hours: int,
    *,
    runner: ProtocolRunner = _default_runner,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[str, ...]:
    """The rows of the last ``window_hours`` hours, one string per printed line."""
    argv = [
        sys.executable,
        "-m",
        ROWS_LOADER_MODULE,
        "--reader",
        reader,
        "--window-hours",
        str(window_hours),
    ]
    return _run(argv, reader, runner, timeout_s)


def _run(
    argv: list[str], reader: str, runner: ProtocolRunner, timeout_s: float
) -> tuple[str, ...]:
    try:
        done = runner(argv, timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LedgerRowsUnreadableError(
            f"the rows loader did not run for reader {reader}: {type(exc).__name__}"
        ) from exc
    if done.returncode != 0:
        raise LedgerRowsUnreadableError(
            f"the rows loader exited {done.returncode} for reader {reader}: {done.stderr.strip()[-200:]}"
        )
    return tuple(done.stdout.splitlines())
