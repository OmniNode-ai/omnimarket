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
    ModelProveRunningLinesPayload,
    ModelProveRunningLinesWritten,
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


#: What the bus append stamps on every row: ``" | req=<uuid>"``.
ROW_STAMP_CHARS = len(" | req=") + 36


def _chunks(rows: list[str], max_chars: int) -> list[list[str]]:
    """Consecutive runs of ``rows`` whose stamped text fits ``max_chars``; one run when they all fit. A row that alone
    exceeds the limit goes by itself, so the bus answers its own refusal for it."""
    chunks: list[list[str]] = [[]]
    used = 0
    for row in rows:
        size = len(row) + ROW_STAMP_CHARS + 1
        if chunks[-1] and used + size > max_chars:
            chunks.append([])
            used = 0
        chunks[-1].append(row)
        used += size
    return chunks


def write_line_rows(
    step_json: str,
    *,
    lane: str,
    ticket: str,
    written_at: str,
    append: Callable[[str, str, str], tuple[int, str]],
    max_chars: int,
) -> tuple[int, str]:
    """The ``write-line-row`` step: build the row of every line of the pass from the batch step's payload and append
    them together through ``append(rows, step_key, lane)``, in the order the lines were given. The rows are the
    ones each line was given when it was written alone (its step key is ``<step key>/<case>/<key>``). One append
    carries them all unless they pass ``max_chars`` (the bus request's own size bound), and then one append carries
    each consecutive run that fits. ``written_at`` is the step input's own time when it carries one, else the
    caller's clock. Returns the exit code and the one line to print (the answer, or the first refusal's reason)."""
    step = ModelProveRunningBatchStepInput.model_validate_json(step_json)
    lines = ModelProveRunningLinesPayload.model_validate_json(step.payload).lines
    stamp = step.written_at or written_at
    rows: list[str] = []
    kinds: list[EnumProveRunningResultKind] = []
    for line in lines:
        request = ModelProveRunningLineRowRequest(
            **line.model_dump(),
            step_key=f"{step.step_key}/{line.case.value}/{line.key}",
            lane=lane,
            ticket=ticket,
            written_at=stamp,
            batch_id=step.batch_id,
        )
        rows.append(HandlerProveRunningLineRows().handle(request).row)
        kinds.append(line_result_kind(request))
    chunks = _chunks(rows, max_chars) if rows else []
    for index, chunk in enumerate(chunks):
        key = step.step_key if len(chunks) == 1 else f"{step.step_key}/chunk/{index}"
        code, error = append("\n".join(chunk), key, lane)
        if code != 0:
            return code, f"ledger append exited {code}: {error}"
    return 0, ModelProveRunningLinesWritten(
        rows=len(rows), appends=len(chunks), results=tuple(kinds)
    ).model_dump_json()
