# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Apply the delegation adapter's contract-owned client verdict table (OMN-21098)."""

from functools import cache
from pathlib import Path

import yaml

from omnimarket.models.claude_code_binding import (
    EnumClientMaxMode,
    EnumClientVerdictAction,
)
from omnimarket.nodes.node_delegation_client_verdict_compute.models.model_delegation_client_verdict import (
    EnumDelegationClientOutcome,
    EnumDelegationClientVerdictRow,
    ModelDelegationClientVerdictRequest,
    ModelDelegationClientVerdictResult,
    ModelDelegationClientVerdictRow,
)

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"

# Outcomes observed without an admission event: the column is the client maximum.
_UNECHOED_OUTCOMES = frozenset(
    {
        EnumDelegationClientOutcome.REFUSED_BEFORE_PUBLISH,
        EnumDelegationClientOutcome.ADMISSION_UNKNOWN,
    }
)

_MODE_RANK = {
    EnumClientMaxMode.OBSERVE: 0,
    EnumClientMaxMode.SELECTIVE: 1,
    EnumClientMaxMode.REQUIRED: 2,
}


def validate_verdict_table(rows: tuple[ModelDelegationClientVerdictRow, ...]) -> None:
    """Reject duplicate cells and wildcard selectors overlapping specific cells."""
    seen: dict[
        tuple[EnumDelegationClientVerdictRow, EnumClientMaxMode], set[bool | None]
    ] = {}
    for cell in rows:
        selectors = seen.setdefault((cell.row, cell.mode), set())
        if cell.allowlisted in selectors or (
            selectors and (cell.allowlisted is None or None in selectors)
        ):
            raise ValueError(f"ambiguous verdict row for {cell.row} at {cell.mode}")
        selectors.add(cell.allowlisted)


@cache
def load_verdict_table() -> tuple[ModelDelegationClientVerdictRow, ...]:
    """Load and validate this node's immutable contract table once."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    if not isinstance(contract, dict) or not isinstance(
        contract.get("verdict_table"), list
    ):
        raise ValueError("contract verdict_table must be a list")
    rows = tuple(
        ModelDelegationClientVerdictRow.model_validate(row)
        for row in contract["verdict_table"]
    )
    validate_verdict_table(rows)
    return rows


def _classify(
    request: ModelDelegationClientVerdictRequest,
) -> EnumDelegationClientVerdictRow:
    # An echo on an unechoed outcome, or none on an outcome that implies an
    # admission event, contradicts itself and is never mapped to a column.
    if (request.outcome in _UNECHOED_OUTCOMES) != (request.effective_mode is None):
        return EnumDelegationClientVerdictRow.INCONSISTENT_OBSERVATION
    if (
        request.effective_mode is not None
        and _MODE_RANK[request.effective_mode] > _MODE_RANK[request.client_max_mode]
    ):
        return EnumDelegationClientVerdictRow.EFFECTIVE_MODE_ABOVE_MAX
    if request.outcome == EnumDelegationClientOutcome.TERMINAL:
        if (
            request.client_call_key == ""
            or request.echoed_client_call_key != request.client_call_key
            or request.echoed_request_fingerprint != request.request_fingerprint
        ):
            return EnumDelegationClientVerdictRow.TERMINAL_IDENTITY_MISMATCH
        if (
            request.terminal_status == "completed"
            and request.terminal_quality_gate_passed
            and request.terminal_failure_cause == ""
        ):
            return EnumDelegationClientVerdictRow.TERMINAL_ACCEPTED
        return EnumDelegationClientVerdictRow.TERMINAL_REJECTED
    return EnumDelegationClientVerdictRow(request.outcome.value)


class HandlerDelegationClientVerdict:
    """Deterministic compute over observations and a validated contract snapshot."""

    def __init__(
        self, table: tuple[ModelDelegationClientVerdictRow, ...] | None = None
    ) -> None:
        if table is None:
            table = load_verdict_table()
        else:
            validate_verdict_table(table)
        self._table = table

    def handle(
        self, request: ModelDelegationClientVerdictRequest
    ) -> ModelDelegationClientVerdictResult:
        """Choose the echoed column, classify observations and apply one table cell."""
        mode = (
            request.effective_mode
            if request.effective_mode is not None
            else request.client_max_mode
        )
        row = _classify(request)
        action = EnumClientVerdictAction.REFUSE
        reason = (
            f"no verdict row for {row} at {mode}; "
            f"correlation_id {request.correlation_id}"
        )
        for cell in self._table:
            if (
                cell.row == row
                and cell.mode == mode
                and (
                    cell.allowlisted is None
                    or cell.allowlisted == request.task_type_allowlisted
                )
            ):
                action = cell.action
                reason = cell.reason.format_map(
                    {
                        "correlation_id": request.correlation_id,
                        "admission_reason": request.admission_reason,
                        "terminal_status": request.terminal_status,
                        "terminal_failure_cause": request.terminal_failure_cause,
                    }
                )
                break
        return ModelDelegationClientVerdictResult(
            action=action,
            reason=reason,
            row=row,
            mode=mode,
            correlation_id=request.correlation_id,
        )
