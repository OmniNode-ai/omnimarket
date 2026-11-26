# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the one ledger row a prove-running workflow ends in. Pure: no clock (the caller supplies the time), no I/O."""

from __future__ import annotations

import re

from omnimarket.nodes.node_prove_running_workflow.models.model_prove_running import (
    EnumProveRunningOutcome,
    EnumProveRunningResultKind,
    ModelProveRunningChange,
    ModelProveRunningRowRequest,
    ModelProveRunningRowResult,
)

ACTOR = "node_prove_running_workflow"
ALARM_KEY = "prove-running-deadline"
_CELL_MAX = 300


def _cell(text: str) -> str:
    """One ledger cell: no pipe, no line break, bounded; an empty value reads ``none``."""
    flat = re.sub(r"\s+", " ", text.replace("|", "/")).strip()
    return flat[:_CELL_MAX] or "none"


def _token(text: str) -> str:
    """A key=value value with no spaces."""
    return re.sub(r"\s+", "-", _cell(text))


def alarm_result_kind(change: ModelProveRunningChange) -> EnumProveRunningResultKind:
    """A change still not running at the deadline is blocked when its line names an owner, else it is a next step."""
    return (
        EnumProveRunningResultKind.BLOCKED
        if change.owner.strip()
        else EnumProveRunningResultKind.NEXT_STEP
    )


def alarm_next_step(change: ModelProveRunningChange) -> str:
    return change.next_step.strip() or (
        f"roll {change.target} to {change.merge_sha[:12]} and read it again at the next hourly run"
    )


class HandlerProveRunningRows:
    """A STATUS row ``result=proven pr= installed= observed= read_at=`` when proved; an alarm row naming the change and
    the last reading, ``result=blocked owner=`` or ``result=next-step next_step=``, when the deadline passed with the
    change still not running. One row per change."""

    def handle(
        self, request: ModelProveRunningRowRequest
    ) -> ModelProveRunningRowResult:
        change = request.change
        reading = request.reading
        head = (
            f"{request.written_at} | STATUS | lane={request.lane} | ticket={request.ticket}"
            f" | actor={ACTOR} | model=none"
        )
        workflow_cells = (
            f"workflow={_token(change.workflow_id)} | step={_token(request.step_key)}"
        )
        if request.batch_id:
            workflow_cells += f" | batch={_token(request.batch_id)}"
        if request.outcome is EnumProveRunningOutcome.PROVED:
            if reading is None or not reading.running:
                raise ValueError("a STATUS row is written only from a running reading")
            row = (
                f"{head} | result={EnumProveRunningResultKind.PROVEN.value} | pr={change.key} | installed={_token(reading.host)}:{_token(reading.revision)}"
                f" | observed={_cell(reading.observed)} | read_at={_token(reading.read_at)} | {workflow_cells}"
                f" | {change.key} read running on {_cell(reading.host)} after the durable wait"
            )
        else:
            last = (
                "none"
                if reading is None
                else _cell(
                    f"running={reading.running} installed={reading.installed} on {reading.host}"
                    f" at {reading.revision}: {reading.observed} {reading.detail}"
                )
            )
            read_at = "none" if reading is None else _token(reading.read_at)
            kind = alarm_result_kind(change)
            carried = (
                f"owner={_token(change.owner)}"
                if kind is EnumProveRunningResultKind.BLOCKED
                else f"next_step={_token(alarm_next_step(change))}"
            )
            row = (
                f"{head} | result={kind.value} | {carried} | src=alarm | alarm={ALARM_KEY} | pr={change.key} | sha={change.merge_sha}"
                f" | target={_token(change.target)} | last_reading={last} | read_at={read_at} | {workflow_cells}"
                f" | {change.key}@{change.merge_sha} still not running on {_cell(change.target)} after the declared deadline"
            )
        return ModelProveRunningRowResult(row=row)
