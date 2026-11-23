# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A non-zero host reconcile exit is written as one alarm row and one alert on the first failure (OMN-26216).

Before this module a failed reconcile left its verdict in the run receipt only: the exit-2 path sent one
alert when a Slack channel or an alert command was configured, and every other non-zero exit (3 indeterminate,
4 lock held, 5 timeout, 6 refused) left nothing a reader could find. A failed reconcile was found ten hours
later by an unrelated pass. The row is a FRICTION row of the shape the lab alarm writes (``class=``,
``condition=``, ``subject=``, ``src=alarm``), so the existing alarm-owner responder reads it by its
``condition`` and ``subject`` cells and dispatches an owner once nobody covers it; no new transport exists.
"""

from __future__ import annotations

import shlex
import subprocess
import uuid
from datetime import UTC, datetime

TICKET = "OMN-26216"
ALARM_ACTOR = "launchd:onex-host-reconcile"
CONDITION = "host_reconcile_failure"
LEDGER_LANE = "onex-host-reconcile"
APPEND_TIMEOUT_S = 30
_COST = "~0.25 lane-hours per failed reconcile that nobody reads"


def _cell(value: object, limit: int = 300) -> str:
    """One ledger cell: no pipe, no line break, bounded."""
    return " ".join(str(value).replace("|", "/").split())[:limit]


def failure_friction_row(
    *, at: datetime, host: str, exit_code: int, detail: str, receipt: str
) -> str:
    """The FRICTION row one failed reconcile writes; ``subject`` is the host, so a repeat is one cause key."""
    stamp = at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp} | FRICTION | lane={LEDGER_LANE} | actor={ALARM_ACTOR} | model=none"
        f" | existing={TICKET} | cost={_COST} | class=host-reconcile-failure"
        f" | src=alarm | condition={CONDITION} | subject={_cell(host, 80)}"
        f" | exit={exit_code} | receipt={_cell(receipt, 200)}"
        f" | onex-host-reconcile exited {exit_code} on {_cell(host, 80)}: {_cell(detail)}"
    )


def failure_alert_text(
    *, host: str, exit_code: int, detail: str, receipt: str, root: str
) -> str:
    return (
        f"onex-host-reconcile exited {exit_code} on {host}: {detail}\n"
        f"receipt: {receipt}\nhost root: {root}"
    )


def append_friction(*, append_command: str, row: str, host: str) -> str | None:
    """Append ``row`` through the bus append command; the reason it could not be written, else None.

    ``append_command`` is the host's bus ledger writer (``ONEX_RECONCILE_LEDGER_APPEND_CMD``, for example
    ``onex work-ledger append --bus-lane dev``); this adds the writer's attribution flags and sends the row on
    stdin. With no append command nothing is written, as with no alert command. No ledger file is named or read.
    A writer that fails never changes the reconcile's exit.
    """
    if not append_command.strip():
        return None
    try:
        done = subprocess.run(
            [
                *shlex.split(append_command),
                "--lane",
                LEDGER_LANE,
                "--host",
                host,
                "--request-id",
                str(uuid.uuid4()),
                "--rows-file",
                "-",
            ],
            input=row + "\n",
            capture_output=True,
            text=True,
            timeout=APPEND_TIMEOUT_S,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return f"{type(exc).__name__}: {_cell(exc, 160)}"
    if done.returncode:
        return f"ledger writer exited {done.returncode}: {_cell(done.stderr or done.stdout, 160)}"
    return None


__all__ = [
    "ALARM_ACTOR",
    "CONDITION",
    "append_friction",
    "failure_alert_text",
    "failure_friction_row",
]
