# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One alarm per cause, to the live orchestrator's inbox and the operator's push channel (OMN-26483).

Failure rows were written and nothing read them. On 2026-11-22 the landing controller refused every tick for
about an hour with its refusal kind only in its own ticks.jsonl, and the throughput tick sent 112 identical MSGs
to a lane name that did not exist (0 ACKs). This node reads four failure classes and delivers each cause once:

``controller-refusal``  the controller refused three ticks in a row (ticks.jsonl); names the kind and first tick
``held-roll-command``   a held roll command older than 30 minutes: a typed gap, the gate state is not in rows
``ledger-refused``      a TERMINAL or STATUS row of a lane that ended ledger-refused (exit 70)
``main-red``            the STATUS rows the red-main alarm node already writes; its detection is not rebuilt

The cause key is class plus subject. One alarm row and one delivery exist per key until a resolved row is
written, and a resolved row carries the minutes the cause lasted. The orchestrator lane is resolved from the live
open CLAIM rows when the alarm is delivered, never from a name written into this code; an addressee that resolves to
no live lane is itself an alarm to the push channel. The push channel is the ``alert_command`` field the host
reconcile alarm uses. A source that parses to zero rows refuses loudly (the 2026-11-22 refusal was exactly
"positive control failed: zero CLAIM rows parsed"), and each fire first feeds a synthetic failure through in-memory
sinks and refuses if one alarm does not reach both destinations.
"""

from __future__ import annotations

import contextlib
import shlex
import socket
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from omnimarket.handlers.ledger_row_grammar import Row, parse_row
from omnimarket.nodes.node_failure_alarm_router_effect.causes import (
    ALARM_CELL,
    HELD_ROLL,
    HELD_ROLL_GAP,
    RESOLVED_CELL,
    ROUTER_LANE,
    Cause,
    Tick,
    cell,
    controller_cause,
    lasted_minutes,
    live_orchestrator,
    open_and_resolved,
    parse_ticks,
    resolution,
    row_causes,
    stamp_of,
)
from omnimarket.nodes.node_failure_alarm_router_effect.models import (
    ModelAlarmGap,
    ModelCauseAlarm,
    ModelCauseResolved,
    ModelFailureAlarmRequest,
    ModelFailureAlarmResult,
)

TICKET = "OMN-26483"
ACTOR = "node:node_failure_alarm_router_effect"
COMMAND_TIMEOUT_S = 60
_SELF_CHECK_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_SELF_CHECK_ORCHESTRATOR = "orchestrator-selfcheck"
_SELF_CHECK_LANE = "selfcheck-synthetic"


class PositiveControlFailedError(RuntimeError):
    """A source that should hold rows parsed to none; the fire refuses rather than read a clean bill."""


class SelfCheckFailedError(RuntimeError):
    """A synthetic failure row did not reach both destinations."""


class FailureAlarmPorts(Protocol):
    def read_rows(self) -> list[str]: ...
    def read_ticks(self) -> list[str] | None:
        """The controller's tick lines, or None when no ticks source is configured."""

    def append(self, request_id: str, rows: list[str]) -> None: ...
    def push(self, text: str) -> None: ...


class CommandPorts:
    """Files and commands the request names: rows from a file or a command, the push channel by command."""

    def __init__(self, request: ModelFailureAlarmRequest) -> None:
        if not request.alert_command.strip():
            raise ValueError(
                "no operator push channel: alert_command is empty, so an alarm could not be delivered"
            )
        self._request = request
        self._host = request.host or socket.gethostname()

    def read_rows(self) -> list[str]:
        request = self._request
        if request.ledger_rows_file:
            return (
                Path(request.ledger_rows_file).read_text(encoding="utf-8").splitlines()
            )
        if request.ledger_rows_command.strip():
            done = subprocess.run(
                shlex.split(request.ledger_rows_command),
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_S,
                check=False,
            )
            if done.returncode:
                raise RuntimeError(
                    f"ledger rows command exited {done.returncode}: {cell(done.stderr, 160)}"
                )
            return done.stdout.splitlines()
        raise ValueError(
            "no ledger rows source: name ledger_rows_file or ledger_rows_command"
        )

    def read_ticks(self) -> list[str] | None:
        request = self._request
        if not request.controller_ticks_path:
            return None
        lines = (
            Path(request.controller_ticks_path).read_text(encoding="utf-8").splitlines()
        )
        return lines[-request.controller_tick_tail :]

    def append(self, request_id: str, rows: list[str]) -> None:
        command = self._request.ledger_append_command
        if not command.strip():
            raise ValueError("no ledger writer: ledger_append_command is empty")
        done = subprocess.run(
            [
                *shlex.split(command),
                "--lane",
                ROUTER_LANE,
                "--host",
                self._host,
                "--request-id",
                request_id,
                "--rows-file",
                "-",
            ],
            input="\n".join(rows) + "\n",
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )
        if done.returncode:
            raise RuntimeError(
                f"ledger writer exited {done.returncode}: {cell(done.stderr or done.stdout, 160)}"
            )

    def push(self, text: str) -> None:
        done = subprocess.run(
            [*shlex.split(self._request.alert_command), text],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )
        if done.returncode:
            raise RuntimeError(
                f"push command exited {done.returncode}: {cell(done.stderr or done.stdout, 160)}"
            )


class _MemoryPorts:
    """The self-check's sinks: nothing leaves the process."""

    def __init__(self, rows: list[str]) -> None:
        self.rows = rows
        self.appended: list[str] = []
        self.pushed: list[str] = []

    def read_rows(self) -> list[str]:
        return list(self.rows)

    def read_ticks(self) -> list[str] | None:
        return []

    def append(self, request_id: str, rows: list[str]) -> None:
        self.appended.extend(rows)
        self.rows.extend(rows)

    def push(self, text: str) -> None:
        self.pushed.append(text)


def _deliver_message(
    ports: FailureAlarmPorts, request_id: str, rows: list[str], push_text: str
) -> None:
    """Push first, then write the rows: a failed push leaves no row, so the next fire retries it."""
    ports.push(push_text)
    ports.append(request_id, rows)


def _alarm_rows(
    cause: Cause, orchestrator: str | None, now: datetime, request_id: str
) -> list[str]:
    at = stamp_of(now)
    status = (
        f"{at} | STATUS | lane={ROUTER_LANE} | actor={ACTOR} | model=none | src=alarm"
        f" | alarm={ALARM_CELL} | cause={cause.key} | class={cause.failure_class}"
        f" | subject={cell(cause.subject, 120)} | kind={cell(cause.kind)} | first_at={cause.first_at}"
        f" | to={orchestrator or 'none'} | req={request_id}"
        f" | {cause.failure_class} {cell(cause.subject, 120)}: {cell(cause.kind)}, first seen {cause.first_at}"
    )
    rows = [status]
    if orchestrator is not None:
        rows.append(
            f"{at} | MSG | from={ROUTER_LANE} | to={orchestrator} | id={at}-{ROUTER_LANE}-{request_id[:8]}"
            f" | req={request_id} | {orchestrator}: {cause.failure_class} on {cell(cause.subject, 120)}:"
            f" {cell(cause.kind)}; first {cause.first_at}"
        )
    return rows


def _push_text(cause: Cause, orchestrator: str | None) -> str:
    text = f"ALARM {cause.failure_class} {cell(cause.subject, 120)}: {cell(cause.kind)}; first {cause.first_at}"
    if orchestrator is None:
        return f"{text}; no live orchestrator lane, so no inbox message was written"
    return f"{text}; inbox message sent to {orchestrator}"


def _resolved_row(
    key: str,
    failure_class: str,
    subject: str,
    first_at: str,
    resolved: datetime,
    now: datetime,
) -> str:
    minutes = lasted_minutes(first_at, resolved)
    return (
        f"{stamp_of(now)} | STATUS | lane={ROUTER_LANE} | actor={ACTOR} | model=none | src=alarm"
        f" | alarm={RESOLVED_CELL} | cause={key} | class={failure_class} | subject={cell(subject, 120)}"
        f" | first_at={first_at} | resolved_at={stamp_of(resolved)} | lasted_minutes={minutes}"
        f" | {failure_class} {cell(subject, 120)} ended after {minutes} minutes"
    )


def _parse_rows(lines: list[str]) -> list[Row]:
    """Parse the ledger lines; zero rows from a non-empty source refuses loudly."""
    nonblank = [line for line in lines if line.strip()]
    if not nonblank:
        raise PositiveControlFailedError(
            "positive control failed: the ledger rows source is empty"
        )
    rows = [row for line in nonblank if (row := parse_row(line)) is not None]
    if not rows:
        raise PositiveControlFailedError(
            f"positive control failed: zero ledger rows parsed of {len(nonblank)} non-empty lines"
        )
    if any(" | CLAIM | " in line for line in nonblank) and not any(
        row.row_type == "CLAIM" for row in rows
    ):
        raise PositiveControlFailedError(
            "positive control failed: zero CLAIM rows parsed"
        )
    return rows


class HandlerFailureAlarmRouter:
    """Read the failure classes and deliver one alarm per cause to the orchestrator inbox and the push channel."""

    def __init__(self, *, ports: FailureAlarmPorts | None = None) -> None:
        self._ports = ports
        self._refusals_pushed: set[str] = set()

    def self_check(self) -> None:
        """Feed a synthetic failure row through in-memory sinks; raise unless one alarm reaches both."""
        lane = _SELF_CHECK_ORCHESTRATOR
        at = stamp_of(_SELF_CHECK_NOW)
        sinks = _MemoryPorts(
            [
                f"{at} | CLAIM | lane={lane} | actor=self-check | model=none | ticket={TICKET}"
                f" | est ~0 lane-hours; displaces nothing; ({TICKET}) | synthetic live orchestrator",
                f"{at} | TERMINAL | lane={_SELF_CHECK_LANE} | ticket={TICKET} | outcome=not-started"
                " | synthetic: status=ledger-refused exit 70",
            ]
        )
        request = ModelFailureAlarmRequest(now=_SELF_CHECK_NOW)
        result = self._evaluate(sinks, request, _SELF_CHECK_NOW)
        reached_inbox = any(
            f" | MSG | from={ROUTER_LANE} | to={lane} " in row for row in sinks.appended
        )
        if len(result.alarms) != 1 or not reached_inbox or len(sinks.pushed) != 1:
            raise SelfCheckFailedError(
                "self-check failed: a synthetic failure row did not reach both the orchestrator inbox"
                f" and the push channel (alarms={len(result.alarms)}, inbox={reached_inbox},"
                f" pushes={len(sinks.pushed)})"
            )

    def handle(self, request: ModelFailureAlarmRequest) -> ModelFailureAlarmResult:
        self.self_check()
        ports = self._ports or CommandPorts(request)
        now = request.now or datetime.now(UTC)
        try:
            result = self._evaluate(ports, request, now)
        except PositiveControlFailedError as exc:
            self._push_refusal_once(ports, str(exc))
            raise
        return result.model_copy(update={"self_check_passed": True})

    def _push_refusal_once(self, ports: FailureAlarmPorts, message: str) -> None:
        if message in self._refusals_pushed:
            return
        self._refusals_pushed.add(message)
        # The refusal still raises to the caller; the push channel being down is not hidden by it.
        with contextlib.suppress(
            OSError, RuntimeError, ValueError, subprocess.TimeoutExpired
        ):
            ports.push(f"ALARM failure alarm router refused to read: {message}")

    def _evaluate(
        self, ports: FailureAlarmPorts, request: ModelFailureAlarmRequest, now: datetime
    ) -> ModelFailureAlarmResult:
        rows = _parse_rows(ports.read_rows())
        tick_lines = ports.read_ticks()
        gaps = [ModelAlarmGap(failure_class=HELD_ROLL, reason=HELD_ROLL_GAP)]
        ticks: list[Tick] = []
        if tick_lines is None:
            gaps.append(
                ModelAlarmGap(
                    failure_class="controller-refusal",
                    reason="no controller ticks source is configured (controller_ticks_path is empty)",
                )
            )
        else:
            nonblank = [line for line in tick_lines if line.strip()]
            ticks = parse_ticks(nonblank)
            if nonblank and not ticks:
                raise PositiveControlFailedError(
                    f"positive control failed: zero controller tick lines parsed of {len(nonblank)}"
                )

        open_causes, resolved_at = open_and_resolved(rows)
        since = now - timedelta(hours=request.lookback_hours)
        active = row_causes(rows, since)
        controller = controller_cause(
            ticks, request.controller_subject, request.consecutive_refusals
        )
        if controller is not None:
            active.append(controller)

        failures: list[str] = []
        alarms: list[ModelCauseAlarm] = []
        found = live_orchestrator(rows, request.orchestrator_lane_pattern)
        orchestrator = found[0] if found else None
        orchestrator_source = found[1] if found else "none"
        for cause in active:
            if cause.key in open_causes:
                continue
            ended = resolved_at.get(cause.key)
            if (
                ended is not None
                and datetime.fromisoformat(cause.first_at.replace("Z", "+00:00"))
                <= ended
            ):
                continue
            request_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"onex:failure-alarm:{cause.key}:{cause.first_at}",
                )
            )
            try:
                _deliver_message(
                    ports,
                    request_id,
                    _alarm_rows(cause, orchestrator, now, request_id),
                    _push_text(cause, orchestrator),
                )
            except (
                OSError,
                RuntimeError,
                ValueError,
                subprocess.TimeoutExpired,
            ) as exc:
                failures.append(f"{cause.key}: {cell(exc, 160)}")
                continue
            alarms.append(
                ModelCauseAlarm(
                    cause=cause.key,
                    failure_class=cause.failure_class,
                    subject=cause.subject,
                    first_at=cause.first_at,
                    kind=cause.kind,
                    orchestrator=orchestrator,
                    orchestrator_source=orchestrator_source,
                    delivered_to=(
                        ("orchestrator-inbox", "operator-push")
                        if orchestrator
                        else ("operator-push",)
                    ),
                )
            )

        resolved: list[ModelCauseResolved] = []
        for key, opened in open_causes.items():
            ended_at = resolution(opened, rows, ticks)
            if ended_at is None:
                continue
            row = _resolved_row(
                key,
                opened.failure_class,
                opened.subject,
                opened.first_at,
                ended_at,
                now,
            )
            request_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"onex:failure-alarm-resolved:{key}:{opened.first_at}",
                )
            )
            try:
                ports.append(request_id, [row])
            except (
                OSError,
                RuntimeError,
                ValueError,
                subprocess.TimeoutExpired,
            ) as exc:
                failures.append(f"{key} resolved row: {cell(exc, 160)}")
                continue
            resolved.append(
                ModelCauseResolved(
                    cause=key,
                    resolved_at=stamp_of(ended_at),
                    lasted_minutes=lasted_minutes(opened.first_at, ended_at),
                )
            )
        return ModelFailureAlarmResult(
            alarms=tuple(alarms),
            resolved=tuple(resolved),
            gaps=tuple(gaps),
            failures=tuple(failures),
            rows_parsed=len(rows),
            ticks_parsed=len(ticks),
        )
