# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21264: a run whose every chosen model failed goes back to its caller.

Decision D3 of the delegation requirements specification: an exhausted ladder
ends ``returned_to_caller`` with zero savings, and the calling session does the
task. Measured on receipt ``3861ec7f`` from the 2026-10-20 onboarding walk:
every attempt failed, the run ended ``quality_gate_refused`` carrying the last
provider's error, and ``result.txt`` held text no attempt was accepted for.

The dispatch port is a frozen stub that reports two backends, both failed; no
provider is contacted. The CLI half drives omnibase_infra's own renderer and
run-file writer, the code ``onex delegate`` and /onex:delegate print through.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)
from omnibase_infra.cli.cli_delegate import _write_unattributed_run_files
from omnibase_infra.cli.delegate_human_output import render_delegate_outcome
from omnibase_infra.cli.model_delegate_run_addressing import (
    ModelDelegateRunAddressing,
)
from omnibase_infra.cli.model_delegate_terminal import ModelDelegateTerminal
from omnibase_infra.enums.enum_delegate_locus import EnumDelegateLocus

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
    ProtocolDelegationDispatchPort,
)
from omnimarket.nodes.node_metering_summary_compute import (
    HandlerMeteringSummary,
    ModelCounterfactualBaseline,
    ModelMeteringSummaryRequest,
    ModelMeteringWindow,
    metering_record_from_row,
)
from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    EnumMeteringMeasurement,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_run_savings import (
    modelled_run_savings_columns,
)

pytestmark = pytest.mark.unit

_UNACCEPTED_ANSWER = "def add(a, b):\n    return a - b\n"

#: Two backends the developer chose, both failed: the first was refused by the
#: quality gate, the second errored at the provider.
_BOTH_FAILED: dict[str, object] = {
    "status": "failed",
    "quality_gate_passed": False,
    "content": _UNACCEPTED_ANSWER,
    "error_message": "HTTP 400 model gpt-5-nano is not available",
    "delegated_to": "openai",
    "model_name": "gpt-5-nano",
    "escalation_count": 1,
    "input_tokens": 180,
    "output_tokens": 60,
    "total_tokens": 240,
    "cost_usd": 0.0004,
    "model_cloud_baseline": "claude-sonnet-5-5",
    "attempts": [
        {
            "tier": "byok",
            "backend_id": "byok-openrouter",
            "model_id": "qwen/qwen3.8-27b",
            "quality_gate_passed": False,
            "cost_usd": 0.0004,
            "failure_class": "quality_gate_failed",
            "acceptance_decision": "climb",
            "acceptance_reason": "heuristic_veto",
            "error_message": "WEAK_OUTPUT: the answer subtracts",
        },
        {
            "tier": "byok",
            "backend_id": "byok-openai",
            "model_id": "gpt-5-nano",
            "quality_gate_passed": False,
            "cost_usd": 0.0002,
            "failure_class": "provider_error",
            "acceptance_decision": "terminate",
            "acceptance_reason": "provider_call_failed",
            "error_message": "HTTP 400 model gpt-5-nano is not available",
        },
    ],
}


class _FrozenDispatchPort:
    def __init__(self, result: dict[str, object]) -> None:
        self._result = result

    async def dispatch(self, **_kwargs: object) -> dict[str, object]:
        return json.loads(json.dumps(self._result))


def _terminal() -> ModelDelegateSkillResponse:
    port = cast(ProtocolDelegationDispatchPort, _FrozenDispatchPort(_BOTH_FAILED))
    handler = HandlerDelegateSkill(dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Write add(a, b).",
        task_type="code_generation",
        source="external-client",
        correlation_id=uuid.uuid4(),
    )
    return asyncio.run(handler.handle(request))


def test_every_chosen_model_failing_returns_the_run_to_its_caller() -> None:
    """AC1: cause returned_to_caller, savings 0, cost the sum of the failed attempts."""
    terminal = _terminal()

    assert terminal.status == "failed"
    assert terminal.quality_gate_passed is False
    assert (
        terminal.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.RETURNED_TO_CALLER
    )
    assert terminal.metrics.cost_savings_usd == 0.0
    assert terminal.metrics.cost_usd == pytest.approx(0.0006)
    assert [attempt.backend_id for attempt in terminal.attempts] == [
        "byok-openrouter",
        "byok-openai",
    ]


def _cli_envelope(terminal: ModelDelegateSkillResponse) -> dict[str, object]:
    return {
        "run_id": str(uuid.uuid4()),
        "correlation_id": str(terminal.correlation_id),
        "status": "failed",
        "exit_code": 1,
        "result_model": "omnimarket.ModelDelegateSkillResponse",
        "result": terminal.model_dump(mode="json"),
    }


def test_the_cli_tells_the_caller_to_do_the_task_and_writes_no_unaccepted_answer(
    tmp_path: Path,
) -> None:
    """AC2: a plain message that the session should do the task, and no answer file text."""
    envelope = _cli_envelope(_terminal())

    outcome = render_delegate_outcome(envelope, state_root=tmp_path)
    assert outcome is not None
    assert outcome.succeeded is False
    assert outcome.stdout == ""
    line = outcome.stderr[0]
    assert "returned_to_caller" in line
    assert "no model you chose finished the task" in line
    assert "do the task in this session" in line

    _write_unattributed_run_files(
        envelope=envelope,
        result=ModelDelegateTerminal.model_validate(envelope["result"]),
        state_root=tmp_path,
        addressing=ModelDelegateRunAddressing(
            locus=EnumDelegateLocus.IN_PROCESS, bus="inmemory"
        ),
        prompt="Write add(a, b).",
        task_type="code_generation",
        task_type_resolution="fallback",
    )
    result_txt = tmp_path / "runs" / str(envelope["run_id"]) / "result.txt"
    assert not result_txt.exists() or result_txt.read_text(encoding="utf-8") == ""


_BASELINE = ModelCounterfactualBaseline(
    model="claude-sonnet-5-5",
    pricing_manifest_version="test-manifest",
    price_in_per_1k=Decimal("0.003"),
    price_out_per_1k=Decimal("0.015"),
    as_of="2026-10-01",
    source="pricing_manifest",
)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "correlation_id": str(uuid.uuid4()),
        "created_at": "2026-10-22T12:00:00+00:00",
        "delegated_to": "openai",
        "model_name": "gpt-5-nano",
        "task_type": "code_generation",
        "tokens_input": 180,
        "tokens_output": 60,
        "cost_usd": 0.0006,
        "cost_savings_usd": 0.0,
        "terminal_failure_cause": "returned_to_caller",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "overrides",
    [{}, {"tokens_input": 0, "tokens_output": 0}, {"cost_usd": None}],
    ids=["measured", "no_tokens_recorded", "no_spend_recorded"],
)
def test_metering_counts_a_returned_run_with_zero_savings_not_unknown(
    overrides: dict[str, object],
) -> None:
    """AC3: a returned_to_caller row is a zero saving, never an unknown one."""
    record = metering_record_from_row(_row(**overrides))
    assert record is not None

    summary = HandlerMeteringSummary().handle(
        ModelMeteringSummaryRequest(
            window=ModelMeteringWindow(
                label="run", end=record.occurred_at.replace(year=2027)
            ),
            records=(record,),
            baseline=_BASELINE,
        )
    )

    assert summary.runs_measured == 1
    assert summary.runs_unknown_tokens == 0
    assert summary.runs_unknown_spend == 0
    assert summary.savings_usd == Decimal("0")
    (run,) = summary.by_run
    assert run.measurement is EnumMeteringMeasurement.MEASURED
    assert run.savings_usd == Decimal("0")


def test_the_cause_that_decided_the_last_rung_stays_readable_as_the_reason() -> None:
    """The hand-back keeps the deciding cause, so faults are still told apart."""
    terminal = _terminal()

    assert terminal.terminal_failure_reason == "quality_gate_refused"


def test_the_row_the_dashboard_reads_books_zero_savings_for_a_returned_run() -> None:
    """AC3: the per-row modelled saving the dashboard reads is zero, and measured."""
    columns = modelled_run_savings_columns(_row())

    if columns["savings_measurement"] == "baseline_unresolved":
        pytest.skip("no pricing baseline resolves in this environment")
    assert columns["savings_measurement"] == "measured"
    assert columns["modelled_savings_usd"] is not None
    assert Decimal(str(columns["modelled_savings_usd"])) == Decimal("0")
