# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The GLM usage record compute: one harness run becomes a usage event, and a cap refusal its own event.

The policy is synthetic (see the allowance compute's tests); the run facts are the shape of a harness
delegate receipt and its transcript's retry notes.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.enums.enum_glm_allowance import EnumGlmRefusalScope, EnumGlmRunOutcome
from omnimarket.models.glm_allowance.model_glm_events import (
    ModelGlmRefusalObserved,
    ModelGlmUsageRecorded,
)
from omnimarket.nodes.node_glm_usage_record_compute import (
    HandlerGlmUsageRecord,
    NodeGlmUsageRecordCompute,
)
from omnimarket.nodes.node_glm_usage_record_compute.models.model_glm_usage_record import (
    ModelGlmRunFacts,
    ModelGlmUsageRecordRequest,
    ModelGlmUsageRecordResult,
)
from tests.unit.nodes.node_glm_allowance_compute.test_glm_allowance import NOW, policy

NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_glm_usage_record_compute"
)


def run_facts(**over: Any) -> ModelGlmRunFacts:
    base: dict[str, Any] = {
        "run_id": "run-1",
        "started_at": NOW,
        "duration_s": 60.0,
        "model": "m-big",
        "task_class": "document",
        "lane": "lane-a",
        "outcome": "done",
        "error_class": None,
        "input_tokens": 10_000,
        "cached_input_tokens": 5_000,
        "output_tokens": 2_000,
        "retry_statuses": (),
        "provider_codes": (),
    }
    base.update(over)
    return ModelGlmRunFacts.model_validate(base)


def record(**over: Any) -> ModelGlmUsageRecordResult:
    return HandlerGlmUsageRecord().handle(
        ModelGlmUsageRecordRequest(policy=policy(), run=run_facts(**over))
    )


def test_a_run_becomes_one_usage_event_with_its_credits() -> None:
    usage = record().usage
    assert isinstance(usage, ModelGlmUsageRecorded)
    assert usage.run_id == "run-1"
    assert usage.provider_id == "plan-x"
    assert usage.model == "m-big"
    assert usage.task_class == "document"
    assert usage.lane == "lane-a"
    assert usage.outcome is EnumGlmRunOutcome.DONE
    # (10000*10 + 5000*2 + 2000*30) / 10000 = 17.0; NOW is outside the synthetic peak window, so halved.
    assert usage.credits == pytest.approx(8.5)
    assert usage.peak_factor == 0.5
    assert usage.rated is True


def test_a_run_inside_peak_hours_costs_the_full_rate() -> None:
    peak = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
    assert record(started_at=peak).usage.credits == pytest.approx(17.0)


def test_a_run_that_spent_nothing_still_counts_as_a_call_with_zero_credits() -> None:
    for outcome in ("budget-refused", "unavailable"):
        usage = record(
            outcome=outcome,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            error_class="HarnessBudgetRefused",
        ).usage
        assert usage.credits == 0.0
        assert usage.outcome is EnumGlmRunOutcome(outcome)
    assert (
        record(
            outcome="run-failed", input_tokens=0, cached_input_tokens=0, output_tokens=0
        ).usage.credits
        == 0.0
    )


def test_an_unrated_model_is_marked_and_charged_at_the_dearest_rate() -> None:
    usage = record(model="m-new").usage
    assert usage.rated is False
    assert usage.credits == pytest.approx(
        (10_000 * 10 + 5_000 * 2 + 2_000 * 30) / 10_000 * 0.5
    )


def test_a_clean_run_has_no_refusal() -> None:
    assert record().refusal is None
    assert record(retry_statuses=(None, 500)).refusal is None


def test_a_429_that_retried_into_a_success_is_a_rate_refusal_the_work_survived() -> (
    None
):
    result = record(retry_statuses=(429, 429, 429), duration_s=90.0)
    refusal = result.refusal
    assert isinstance(refusal, ModelGlmRefusalObserved)
    assert refusal.scope is EnumGlmRefusalScope.RATE
    assert refusal.http_status == 429
    assert refusal.count == 3
    assert refusal.run_outcome is EnumGlmRunOutcome.DONE
    assert refusal.observed_at == NOW + timedelta(seconds=90)
    assert refusal.run_id == "run-1"
    assert refusal.task_class == "document"


def test_a_mapped_provider_code_names_the_cap() -> None:
    window = record(retry_statuses=(429,), provider_codes=("W1",)).refusal
    assert window is not None
    assert window.scope is EnumGlmRefusalScope.WINDOW
    assert window.provider_code == "W1"
    week = record(retry_statuses=(429,), provider_codes=("W1", "K1")).refusal
    assert week is not None
    assert week.scope is EnumGlmRefusalScope.WEEK


def test_a_mapped_code_is_a_refusal_even_without_a_429_and_an_unmapped_code_is_not() -> (
    None
):
    by_code = record(provider_codes=("K1",)).refusal
    assert by_code is not None
    assert by_code.scope is EnumGlmRefusalScope.WEEK
    assert by_code.http_status is None
    assert record(provider_codes=("Z9",)).refusal is None
    unmapped = record(retry_statuses=(429,), provider_codes=("Z9",)).refusal
    assert unmapped.scope is EnumGlmRefusalScope.RATE


def test_a_refusal_that_ended_the_run_records_what_became_of_the_work() -> None:
    refusal = record(
        outcome="run-failed",
        error_class="HarnessRunFailed",
        retry_statuses=(429,) * 6,
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
    ).refusal
    assert refusal.run_outcome is EnumGlmRunOutcome.RUN_FAILED
    assert refusal.count == 6


def test_the_events_carry_no_credential_or_prompt() -> None:
    fields = set(ModelGlmUsageRecorded.model_fields) | set(
        ModelGlmRefusalObserved.model_fields
    )
    for banned in ("prompt", "answer", "api_key", "secret", "token_value", "login"):
        assert not any(banned in f for f in fields)


def test_handler_has_the_canonical_signature() -> None:
    assert list(inspect.signature(HandlerGlmUsageRecord.handle).parameters) == [
        "self",
        "request",
    ]
    hints = inspect.get_annotations(HandlerGlmUsageRecord.handle, eval_str=True)
    assert hints["request"] is ModelGlmUsageRecordRequest
    assert hints["return"] is ModelGlmUsageRecordResult
    assert issubclass(NodeGlmUsageRecordCompute, HandlerGlmUsageRecord)


def test_the_contract_declares_the_one_event_carrying_usage_and_refusal_and_the_handler_does_not_hardcode_it() -> (
    None
):
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    dispatch = contract["runtime_dispatch"]
    assert (
        dispatch["command_topic"] == "onex.cmd.omnimarket.glm-usage-record-requested.v1"
    )
    assert (
        dispatch["terminal_events"]["success"]
        == "onex.evt.omnimarket.glm-usage-recorded.v1"
    )
    assert contract["externally_consumed_topics"] == [
        "onex.evt.omnimarket.glm-usage-recorded.v1"
    ]
    assert "event_bus" not in contract
    source = (NODE / "handlers/handler_glm_usage_record.py").read_text()
    assert "onex.evt" not in source
    assert "onex.cmd" not in source
