# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The result row of a NOT RUNNING line that the batch answers itself. Pure: no clock, no I/O.

A line normally ends in the one row its own workflow writes (``HandlerProveRunningRows``). Three cases have no such
workflow in this batch, and each still yields exactly one row, never a silent skip:

* the change's workflow was started by an earlier batch (its id ``<repo>#<n>@<sha>`` is taken): the row says so and
  carries the earlier proof, owner or next step;
* the merge sha of the line was unread, so no workflow could start: the row is blocked (its owner) or a next step;
* the change's workflow ended in failure: the row is a next step naming it.
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.nodes.node_prove_running_workflow.handlers.handler_prove_running_rows import (
    ACTOR,
    _cell,
    _token,
)
from omnimarket.nodes.node_prove_running_workflow.models.model_prove_running import (
    EnumProveRunningLineCase,
    EnumProveRunningOutcome,
    EnumProveRunningResultKind,
    ModelProveRunningBatchStepInput,
    ModelProveRunningLinePayload,
    ModelProveRunningLineRowRequest,
    ModelProveRunningLineWritten,
    ModelProveRunningRowResult,
)

STATUS_COMPLETED = "COMPLETED"
STATUS_RUNNING = "RUNNING"


def _default_next_step(request: ModelProveRunningLinePayload) -> str:
    if request.case is EnumProveRunningLineCase.UNRESOLVED:
        return f"read the merge commit of {request.key} and start its proof at the next hourly run"
    if request.case is EnumProveRunningLineCase.CHILD_FAILED:
        return f"read why the workflow {request.workflow_id} failed and start its proof again"
    return f"roll {request.target} to the merge of {request.key} and read it again at the next hourly run"


def line_result_kind(
    request: ModelProveRunningLinePayload,
) -> EnumProveRunningResultKind:
    """proven when the earlier workflow read the change running; else blocked when the line names an owner; else a
    next step. A workflow that is still running is a next step: await it."""
    earlier = request.earlier
    if (
        request.case is EnumProveRunningLineCase.ALREADY_STARTED
        and earlier is not None
        and earlier.status == STATUS_COMPLETED
        and earlier.result is not None
        and earlier.result.outcome is EnumProveRunningOutcome.PROVED
        and earlier.result.last_reading is not None
        and earlier.result.last_reading.running
    ):
        return EnumProveRunningResultKind.PROVEN
    if (
        earlier is not None
        and earlier.status == STATUS_RUNNING
        and request.case is EnumProveRunningLineCase.ALREADY_STARTED
    ):
        return EnumProveRunningResultKind.NEXT_STEP
    return (
        EnumProveRunningResultKind.BLOCKED
        if request.owner.strip()
        else EnumProveRunningResultKind.NEXT_STEP
    )


class HandlerProveRunningLineRows:
    """One STATUS row ``result=proven|blocked|next-step`` for a line the batch answers itself."""

    def handle(
        self, request: ModelProveRunningLineRowRequest
    ) -> ModelProveRunningRowResult:
        kind = line_result_kind(request)
        earlier = request.earlier
        cells = [
            f"{request.written_at} | STATUS | lane={request.lane} | ticket={request.ticket}"
            f" | actor={ACTOR} | model=none | result={kind.value} | pr={request.key}"
        ]
        if kind is EnumProveRunningResultKind.PROVEN:
            assert earlier is not None
            assert earlier.result is not None
            reading = earlier.result.last_reading
            assert reading is not None
            cells.append(
                f"installed={_token(reading.host)}:{_token(reading.revision)}"
                f" | observed={_cell(reading.observed)} | read_at={_token(reading.read_at)}"
                f" | earlier_proof={_token(earlier.workflow_id)}"
            )
            text = f"{request.key} was already proven by workflow {earlier.workflow_id}; not started again"
        elif kind is EnumProveRunningResultKind.BLOCKED:
            cells.append(f"owner={_token(request.owner)}")
            text = f"{request.key} is blocked on its owner ({request.case.value})"
        else:
            step = request.next_step.strip() or _default_next_step(request)
            cells.append(f"next_step={_token(step)}")
            text = f"{request.key} has one next step ({request.case.value})"
        if earlier is not None:
            cells.append(f"earlier_workflow={_token(earlier.workflow_id)}")
            cells.append(f"earlier_status={_token(earlier.status)}")
            if earlier.result is not None:
                cells.append(f"earlier_outcome={_token(earlier.result.outcome.value)}")
        if request.merge_sha:
            cells.append(f"sha={_token(request.merge_sha)}")
        cells.append(f"target={_token(request.target)}")
        cells.append(f"case={_token(request.case.value)}")
        cells.append(f"step={_token(request.step_key)}")
        if request.batch_id:
            cells.append(f"batch={_token(request.batch_id)}")
        detail = f" ({_cell(request.detail)})" if request.detail else ""
        return ModelProveRunningRowResult(row=" | ".join([*cells, text + detail]))


def write_line_row(
    step_json: str,
    *,
    lane: str,
    ticket: str,
    written_at: str,
    append: Callable[[str, str, str], tuple[int, str]],
) -> tuple[int, str]:
    """The ``write-line-row`` step: build the line's row from the batch step's payload and append it through
    ``append(row, step_key, lane)``. Returns the exit code and the one line to print (the answer, or the error)."""
    step = ModelProveRunningBatchStepInput.model_validate_json(step_json)
    request = ModelProveRunningLineRowRequest(
        **ModelProveRunningLinePayload.model_validate_json(step.payload).model_dump(),
        step_key=step.step_key,
        lane=lane,
        ticket=ticket,
        written_at=written_at,
        batch_id=step.batch_id,
    )
    row = HandlerProveRunningLineRows().handle(request).row
    code, error = append(row, step.step_key, lane)
    if code != 0:
        return code, f"ledger append exited {code}: {error}"
    return 0, ModelProveRunningLineWritten(
        written=True, row=row, result=line_result_kind(request)
    ).model_dump_json()
