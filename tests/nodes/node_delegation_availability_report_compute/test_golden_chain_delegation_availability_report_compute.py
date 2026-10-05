# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18931: provider failures cannot lower a fixed cohort's content score."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_delegation_content_verdict import (
    EnumDelegationContentVerdict as Verdict,
)
from omnibase_core.enums.enum_delegation_operational_outcome import (
    EnumDelegationOperationalOutcome as Outcome,
)
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)
from omnibase_core.models.delegation.wire import ModelDelegationResult
from pydantic import ValidationError

from omnimarket.cost.usage_normalizer import ModelUsageResult, normalize_usage
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_delegation_availability_report_compute.handlers.handler_delegation_availability_report import (
    summarize_single_hop_cohort,
)
from omnimarket.nodes.node_delegation_availability_report_compute.models.model_delegation_cohort_observation import (
    ModelDelegationCohortObservation,
)

pytestmark = pytest.mark.unit


def _observation(
    outcome: Outcome | None,
    *,
    tier: str = "local",
    attempts: int = 1,
    escalations: int = 0,
) -> ModelDelegationCohortObservation:
    correlation_id = uuid4()
    content_outcomes = {
        Outcome.COMPLETED,
        Outcome.QUALITY_REJECTED,
        Outcome.SCHEMA_REJECTED,
    }
    has_content = outcome in content_outcomes
    passed = outcome is Outcome.COMPLETED
    body = (
        {
            "choices": [{"message": {"content": "known valid response"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        if has_content
        else {"error": {"code": 429 if outcome is Outcome.PROVIDER_QUOTA else 503}}
    )
    terminal = None
    if outcome is not None:
        terminal = ModelDelegationResult(
            correlation_id=correlation_id,
            task_type="document",
            model_used="fixture-model",
            endpoint_url="https://provider.example/v1/chat/completions",
            content="known valid response" if has_content else "",
            quality_passed=passed,
            quality_score=(1.0 if passed else 0.2) if has_content else None,
            operational_outcome=outcome,
            content_verdict=(Verdict.USABLE if passed else Verdict.UNUSABLE)
            if has_content
            else Verdict.NOT_APPLICABLE,
            terminal_failure_cause=(
                EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
                if outcome is Outcome.PROVIDER_QUOTA
                else None
            ),
            latency_ms=1,
            fallback_to_claude=False,
            attempts_count=attempts,
            escalation_count=escalations,
            cost_tier_name=tier,
        )
    return ModelDelegationCohortObservation(
        correlation_id=correlation_id,
        backend_tier=tier,
        attempts_count=attempts,
        terminal=terminal,
        usage=normalize_usage(body, "fixture"),
        source_payload_hash=hashlib.sha256(json.dumps(body).encode()).hexdigest(),
    )


@pytest.mark.parametrize("tier", ["local", "cheap_cloud", "cheap_frontier", "claude"])
def test_success_429_and_503_have_separate_denominators(tier: str) -> None:
    observations = [
        _observation(Outcome.COMPLETED, tier=tier),
        _observation(Outcome.PROVIDER_QUOTA, tier=tier),
        _observation(Outcome.PROVIDER_UNAVAILABLE, tier=tier),
    ]
    report = summarize_single_hop_cohort(observations)
    summary = report.tiers[0]
    assert summary.backend_tier == tier
    assert summary.availability_total == 3
    assert summary.availability_failures == 2
    assert summary.availability_counts == {
        Outcome.COMPLETED: 1,
        Outcome.PROVIDER_QUOTA: 1,
        Outcome.PROVIDER_UNAVAILABLE: 1,
    }
    assert summary.correctness_total == 1
    assert summary.correctness_counts == {Verdict.USABLE: 1}
    valid, quota, overload = report.rows
    assert valid.content_verdict is Verdict.USABLE
    assert valid.quality_score == 1.0
    assert valid.usage_source is EnumUsageSource.MEASURED
    for row in (quota, overload):
        assert row.content_verdict is None
        assert row.quality_score is None
        assert row.usage_source is EnumUsageSource.UNKNOWN
    assert [row.source_payload_hash for row in report.rows] == [
        observation.source_payload_hash for observation in observations
    ]
    assert report.excluded_requests == ()


@pytest.mark.parametrize("outcome", [Outcome.TIMEOUT, Outcome.CANCELLED, None])
def test_operational_failure_without_an_answer_has_no_content_verdict(
    outcome: Outcome | None,
) -> None:
    report = summarize_single_hop_cohort([_observation(outcome)])
    assert report.rows[0].availability == (outcome or "no_terminal")
    assert report.rows[0].content_verdict is None
    assert report.rows[0].quality_score is None
    assert report.tiers[0].availability_failures == 1
    assert report.tiers[0].correctness_total == 0
    assert report.tiers[0].correctness_counts == {}


def test_bad_content_is_correctness_evidence_and_tiers_stay_separate() -> None:
    report = summarize_single_hop_cohort(
        [
            _observation(Outcome.QUALITY_REJECTED),
            _observation(Outcome.PROVIDER_UNAVAILABLE, tier="cheap_cloud"),
        ]
    )
    cloud, local = report.tiers
    assert cloud.correctness_total == 0
    assert cloud.availability_failures == 1
    assert local.correctness_counts == {Verdict.UNUSABLE: 1}
    assert local.availability_failures == 0


@pytest.mark.parametrize(("attempts", "escalations"), [(2, 0), (2, 1), (1, 1)])
def test_retried_or_escalated_requests_are_named_exclusions(
    attempts: int,
    escalations: int,
) -> None:
    retried = _observation(
        Outcome.COMPLETED, attempts=attempts, escalations=escalations
    )
    valid = _observation(Outcome.COMPLETED)
    report = summarize_single_hop_cohort([retried, valid])
    assert report.excluded_requests == (retried.correlation_id,)
    assert report.tiers[0].availability_total == 1
    assert report.tiers[0].correctness_total == 1
    assert report.rows[0].correlation_id == valid.correlation_id


def test_repeated_receipts_cannot_inflate_the_cohort() -> None:
    observation = _observation(Outcome.COMPLETED)
    with pytest.raises(ValueError, match="duplicate request"):
        summarize_single_hop_cohort([observation, observation])


def test_usage_estimation_provenance_survives_json_reporting() -> None:
    observation = _observation(Outcome.COMPLETED)
    data = observation.model_dump()
    data["usage"] = normalize_usage(
        {"choices": []}, "fixture", prompt_text="the prompt", response_text="the answer"
    )
    estimated = ModelDelegationCohortObservation.model_validate(data)
    row = json.loads(summarize_single_hop_cohort([estimated]).model_dump_json())[
        "rows"
    ][0]
    assert row["usage_source"] == "estimated"
    assert row["estimation_method"] == "char_length_div_4"
    assert row["source_payload_hash"] == observation.source_payload_hash


@pytest.mark.parametrize("field", ["correlation_id", "attempts_count"])
def test_mismatched_terminal_evidence_is_refused(field: str) -> None:
    data = _observation(Outcome.COMPLETED).model_dump()
    data[field] = uuid4() if field == "correlation_id" else 2
    with pytest.raises(ValidationError, match=r"observed request|match the terminal"):
        ModelDelegationCohortObservation.model_validate(data)


def test_unknown_legacy_outcomes_are_not_guessed_from_gate_failure() -> None:
    data = _observation(Outcome.PROVIDER_UNAVAILABLE).model_dump()
    data["terminal"]["operational_outcome"] = None
    data["terminal"]["content_verdict"] = None
    with pytest.raises(ValidationError, match="typed terminal outcomes"):
        ModelDelegationCohortObservation.model_validate(data)


def test_estimated_usage_without_a_method_is_refused() -> None:
    data = _observation(Outcome.COMPLETED).model_dump()
    data["usage"] = ModelUsageResult(usage_source=EnumUsageSource.ESTIMATED)
    with pytest.raises(ValidationError, match="requires estimation_method"):
        ModelDelegationCohortObservation.model_validate(data)


def test_empty_cohort_has_no_invented_scores() -> None:
    report = summarize_single_hop_cohort([])
    assert report.rows == ()
    assert report.tiers == ()
    assert report.excluded_requests == ()


def test_schema_rejected_response_is_content_evidence() -> None:
    report = summarize_single_hop_cohort([_observation(Outcome.SCHEMA_REJECTED)])
    assert report.tiers[0].correctness_counts == {Verdict.UNUSABLE: 1}
    assert report.tiers[0].availability_failures == 0


def test_caller_cannot_relabel_a_terminal_route_tier() -> None:
    data = _observation(Outcome.COMPLETED).model_dump()
    data["backend_tier"] = "cheap_cloud"
    with pytest.raises(ValidationError, match="terminal route tier"):
        ModelDelegationCohortObservation.model_validate(data)


def test_terminal_construction_failure_is_not_a_content_score() -> None:
    data = _observation(Outcome.COMPLETED).model_dump()
    data["terminal"].update(
        operational_outcome=Outcome.TERMINAL_CONSTRUCTION_FAILED,
        content_verdict=Verdict.UNDETERMINED,
        quality_score=None,
        terminal_failure_reason="terminal_construction_failed",
    )
    observation = ModelDelegationCohortObservation.model_validate(data)
    report = summarize_single_hop_cohort([observation])
    assert report.rows[0].quality_score is None
    assert report.rows[0].content_verdict is None
    assert report.tiers[0].correctness_total == 0
    assert report.tiers[0].availability_failures == 1


@pytest.mark.asyncio
async def test_registered_contract_executes_the_fixed_cohort_json(
    tmp_path: Path,
) -> None:
    import importlib
    from importlib.metadata import entry_points
    from importlib.resources import files

    import yaml
    from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
    from omnibase_core.runtime.runtime_local import RuntimeLocal

    package = "omnimarket.nodes.node_delegation_availability_report_compute"
    entry = next(
        entry
        for entry in entry_points(group="onex.nodes")
        if entry.name == "node_delegation_availability_report_compute"
    )
    assert entry.load().__name__ == package
    contract_path = Path(str(files(package).joinpath("contract.yaml")))
    contract = yaml.safe_load(contract_path.read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.delegation-availability-reported.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    binding = contract["handler"]
    input_module, input_name = binding["input_model"].rsplit(".", 1)
    request_type = getattr(importlib.import_module(input_module), input_name)
    request = request_type.model_validate_json(
        json.dumps(
            {
                "observations": [
                    _observation(outcome).model_dump(mode="json")
                    for outcome in (
                        Outcome.COMPLETED,
                        Outcome.PROVIDER_QUOTA,
                        Outcome.PROVIDER_UNAVAILABLE,
                    )
                ]
            }
        )
    )
    input_path = tmp_path / "cohort.json"
    input_path.write_text(request.model_dump_json())
    runtime = RuntimeLocal(
        workflow_path=contract_path,
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=5,
    )
    assert await runtime.run_async() is EnumWorkflowResult.COMPLETED
    result = runtime.handler_result
    assert result is not None
    output_module, output_name = binding["output_model"].rsplit(".", 1)
    output_type = getattr(importlib.import_module(output_module), output_name)
    round_trip = output_type.model_validate_json(result.model_dump_json())
    assert round_trip == result
    assert round_trip.tiers[0].availability_total == 3
    assert round_trip.tiers[0].availability_failures == 2
    assert round_trip.tiers[0].correctness_total == 1
