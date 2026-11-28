# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A check that reads red after green opens one top-priority ticket, and never a second while it is open (OMN-26726).

``handle(request) -> result`` takes one scheduled run's reading of one check. Red after green (or with no reading yet) opens
one ticket through the ticket node naming the check, the run id, the failing step and the last green run. A red run while that
ticket is still open comments the run on it. A green run after red comments the recovery once and closes nothing: a person
closes it. A ticket that was closed meanwhile is not reused; the next red opens a new one. NOT_MEASURED and PRECONDITION-ABSENT
runs measured nothing and change nothing.

The ticket request has no priority field, so the description carries the line ``Priority: Urgent`` and the title leads with
the check; the ticket node's own create gate decides what it accepts.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Protocol

from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)
from omnimarket.models.ticket_write_io import (
    ModelTicketWriteCompleted,
    ModelTicketWriteRequest,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    EnumCheckVerdict,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_regression_ticket import (
    EnumRegressionAction,
    ModelRegressionCheckState,
    ModelRegressionRunReading,
    ModelRegressionTicketRequest,
    ModelRegressionTicketResult,
)

_RED = (EnumCheckVerdict.FAIL, EnumCheckVerdict.ERROR)
_CLOSED = ("done", "completed", "canceled", "cancelled", "duplicate")


class ProtocolRegressionStateStore(Protocol):
    def load(self, check_id: str) -> ModelRegressionCheckState: ...

    def save(self, check_id: str, state: ModelRegressionCheckState) -> None: ...


class ProtocolTicketWriter(Protocol):
    """The ticket node as the handler uses it: one guarded operation, its result, or an exception."""

    def send(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult: ...


class JsonRegressionStateStore:
    """Per-check state in one JSON file under the tick's state directory."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _all(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return data if isinstance(data, dict) else {}

    def load(self, check_id: str) -> ModelRegressionCheckState:
        return ModelRegressionCheckState.model_validate(self._all().get(check_id, {}))

    def save(self, check_id: str, state: ModelRegressionCheckState) -> None:
        data = self._all()
        data[check_id] = state.model_dump()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        held = self._path.with_suffix(".tmp")
        held.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        held.replace(self._path)


class BusTicketWriter:
    """Send each operation to the ticket node's handler, as a command carrying this lane's identity."""

    def __init__(self, lane: str) -> None:
        from omnimarket.nodes.node_linear_ticket_write_effect.handlers.handler_linear_ticket_write import (
            HandlerLinearTicketWriteEffect,
        )

        self._lane = lane
        self._node = HandlerLinearTicketWriteEffect()

    def send(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult:
        fields = request.model_dump()
        command = ModelTicketWriteRequest(
            **fields,
            correlation_id=uuid.uuid4(),
            idempotency_key=uuid.uuid4().hex,
            requested_by_lane=self._lane,
        )
        answer = asyncio.run(self._node.handle(command))
        if not isinstance(answer, ModelTicketWriteCompleted):
            raise RuntimeError(f"ticket write refused: {answer.terminal_failure_cause}")
        return answer.result


class HandlerRegressionTicket:
    def __init__(
        self,
        *,
        store: ProtocolRegressionStateStore,
        writer: ProtocolTicketWriter,
        parent: str,
        stamp: str = "",
        repo: str | None = None,
    ) -> None:
        self._store = store
        self._writer = writer
        self._parent = parent
        self._stamp = stamp
        self._repo = repo

    def handle(
        self, request: ModelRegressionTicketRequest
    ) -> ModelRegressionTicketResult:
        reading = request.reading
        state = self._store.load(reading.check_id)
        if reading.verdict in _RED:
            return self._red(reading, state)
        if reading.verdict is EnumCheckVerdict.PASS:
            return self._green(reading, state)
        return self._result(
            reading, EnumRegressionAction.NONE, state.open_ticket, "unmeasured"
        )

    def _result(
        self,
        reading: ModelRegressionRunReading,
        action: EnumRegressionAction,
        ticket: str = "",
        detail: str = "",
    ) -> ModelRegressionTicketResult:
        return ModelRegressionTicketResult(
            check_id=reading.check_id,
            run_id=reading.run_id,
            action=action,
            ticket_id=ticket,
            detail=detail,
        )

    def _red(
        self, reading: ModelRegressionRunReading, state: ModelRegressionCheckState
    ) -> ModelRegressionTicketResult:
        check = reading.check_id
        step = reading.failing_step or reading.verdict.value
        ticket = state.open_ticket
        try:
            if ticket and not self._is_closed(ticket):
                self._writer.send(
                    ModelCreateTicketRequest(
                        operation=EnumTicketOperation.COMMENT,
                        ticket_id=ticket,
                        body=(
                            f"Check {check} read red again: run {reading.run_id}, "
                            f"failing step {step}."
                        ),
                    )
                )
                self._store.save(
                    check,
                    state.model_copy(
                        update={
                            "last_verdict": "red",
                            "last_red_run": reading.run_id,
                            "recovery_noted": False,
                        }
                    ),
                )
                return self._result(reading, EnumRegressionAction.COMMENTED, ticket)
            created = self._writer.send(self._create(reading, state, ticket))
            if created.status != "created" or not created.ticket_id:
                raise RuntimeError(
                    f"create answered {created.status}: {','.join(created.finding_codes)}"
                )
        except Exception as error:
            # The reading still counts as red; with no open ticket the next red run tries again.
            self._store.save(
                check,
                state.model_copy(
                    update={
                        "last_verdict": "red",
                        "last_red_run": reading.run_id,
                    }
                ),
            )
            return self._result(
                reading, EnumRegressionAction.FAILED, ticket, type(error).__name__
            )
        self._store.save(
            check,
            state.model_copy(
                update={
                    "last_verdict": "red",
                    "last_red_run": reading.run_id,
                    "open_ticket": created.ticket_id,
                    "recovery_noted": False,
                }
            ),
        )
        return self._result(reading, EnumRegressionAction.OPENED, created.ticket_id)

    def _green(
        self, reading: ModelRegressionRunReading, state: ModelRegressionCheckState
    ) -> ModelRegressionTicketResult:
        action = EnumRegressionAction.NONE
        noted = state.recovery_noted
        detail = ""
        if state.open_ticket and not state.recovery_noted:
            try:
                self._writer.send(
                    ModelCreateTicketRequest(
                        operation=EnumTicketOperation.COMMENT,
                        ticket_id=state.open_ticket,
                        body=(
                            f"Check {reading.check_id} read green again: run "
                            f"{reading.run_id}. This ticket stays open until a person closes it."
                        ),
                    )
                )
                action = EnumRegressionAction.RECOVERY_COMMENTED
                noted = True
            except Exception as error:
                action = EnumRegressionAction.FAILED
                detail = type(error).__name__
        self._store.save(
            reading.check_id,
            state.model_copy(
                update={
                    "last_verdict": "green",
                    "last_green_run": reading.run_id,
                    "recovery_noted": noted,
                }
            ),
        )
        return self._result(reading, action, state.open_ticket, detail)

    def _is_closed(self, ticket: str) -> bool:
        read = self._writer.send(
            ModelCreateTicketRequest(
                operation=EnumTicketOperation.READ, ticket_id=ticket
            )
        )
        return read.ticket_status.strip().lower() in _CLOSED

    def _create(
        self,
        reading: ModelRegressionRunReading,
        state: ModelRegressionCheckState,
        closed_ticket: str,
    ) -> ModelCreateTicketRequest:
        check = reading.check_id
        step = reading.failing_step or reading.verdict.value
        green = state.last_green_run or "none on record"
        lines = [
            "Priority: Urgent (top item: a delegation check that reads red is a regression)",
            "",
            "Gate: OmniNode-ai/omnimarket ci.yml",
            "",
            f"Check: {check}",
            f"Run: {reading.run_id}",
            f"Failing step: {step}",
            f"Last green run: {green}",
        ]
        if closed_ticket:
            lines.append(f"Earlier ticket for this check, closed: {closed_ticket}")
        lines += [
            "",
            "## Acceptance criteria",
            "",
            f"- [ ] AC1: check {check} reads green on a scheduled run -- falsifier: the next scheduled run of {check} reads red at step {step}",
            f"- [ ] AC2: integrated: the scheduled probe run on the lab reads {check} green -- falsifier: the probe tick run on a lab host reads {check} FAIL, read back from its last-run.json",
        ]
        description = "\n".join(lines)
        if self._stamp:
            description = f"{self._stamp}\n\n{description}"
        return ModelCreateTicketRequest(
            operation=EnumTicketOperation.CREATE,
            title=f"[P1] Delegation check {check} turned red at {step} (run {reading.run_id})",
            description=description,
            parent=self._parent,
            repo=self._repo,
        )


def failing_step_of(clauses: list[tuple[str, bool]], error: str | None) -> str:
    """The first failed clause's name, else the run's error, else blank."""
    for name, ok in clauses:
        if not ok:
            return name
    return (error or "")[:200]


__all__ = [
    "BusTicketWriter",
    "HandlerRegressionTicket",
    "JsonRegressionStateStore",
    "ProtocolRegressionStateStore",
    "ProtocolTicketWriter",
    "failing_step_of",
]
