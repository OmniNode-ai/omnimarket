# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13367 reproducible judge verdict event tests."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.events.delegation_judge_verdict import (
    EnumDelegationJudgeVerdict,
    ModelDelegationJudgeVerdictEvent,
    build_delegation_judge_verdict_event,
    sha256_text,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    JUDGE_VERDICT_TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_ROOT = Path(__file__).resolve().parents[3]
_RUBRIC_PATH = _ROOT / "src/omnimarket/configs/delegation_judge_rubrics.v1.yaml"
_TASK_CONTRACT_PATH = _ROOT / "src/omnimarket/configs/task_class_contracts.v1.yaml"
_QUALITY_GATE_CONTRACT_PATH = (
    _ROOT / "src/omnimarket/nodes/node_delegation_quality_gate_reducer/contract.yaml"
)
_PROJECTION_CONTRACT_PATH = (
    _ROOT / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
)
_MIGRATION_PATH = (
    _ROOT / "src/omnimarket/nodes/node_projection_delegation/migrations/"
    "0016_delegation_judge_verdict_events.sql"
)
_CORPUS_PATH = Path(__file__).parent / "judge_verdict_calibration_corpus.yaml"

_IDENTITY_FIELDS = {
    "judge_model",
    "judge_model_version",
    "judge_provider",
    "rubric_id",
    "rubric_hash",
    "prompt_hash",
    "input_hash",
    "temperature",
    "judge_node_version",
    "reasoning_hash",
    "verdict",
    "actual_score",
}


def _base_event_kwargs() -> dict[str, object]:
    return {
        "correlation_id": uuid4(),
        "task_type": "research",
        "judge_model": "fixture-judge",
        "judge_model_version": "2026-06-20",
        "judge_provider": "fixture",
        "rubric_id": "delegation_non_verifiable_v1",
        "rubric_hash": (
            "sha256:321240472e3b52d21fd8cb9b9b7639bb73161a2945adf580948e86275ce20681"
        ),
        "prompt_hash": sha256_text("prompt"),
        "input_hash": sha256_text("input"),
        "temperature": 0.0,
        "judge_node_version": "delegation-judge-verdict/1.0.0",
        "reasoning_hash": sha256_text("reasoning"),
        "verdict": "pass",
        "actual_score": 0.91,
        "event_hash": (
            "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ),
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "missing_field",
    ["rubric_hash", "prompt_hash", "judge_model", "temperature"],
)
def test_required_identity_fields_are_required(missing_field: str) -> None:
    payload = _base_event_kwargs()
    payload.pop(missing_field)

    with pytest.raises(ValidationError):
        ModelDelegationJudgeVerdictEvent(**payload)


@pytest.mark.unit
def test_identity_bundle_contains_required_fields_and_replay_hash() -> None:
    event = build_delegation_judge_verdict_event(
        correlation_id=UUID("11111111-1111-4111-8111-111111111111"),
        task_type="research",
        judge_model="fixture-judge",
        judge_model_version="2026-06-20",
        judge_provider="fixture",
        rubric_id="delegation_non_verifiable_v1",
        rubric_hash=str(_base_event_kwargs()["rubric_hash"]),
        prompt="prompt",
        judged_input="input",
        temperature=0.0,
        judge_node_version="delegation-judge-verdict/1.0.0",
        reasoning="reasoning",
        verdict=EnumDelegationJudgeVerdict.PASS,
        actual_score=0.91,
    )

    assert set(event.identity_bundle()) >= _IDENTITY_FIELDS
    assert event.event_hash == event.compute_event_hash()

    replay = build_delegation_judge_verdict_event(
        correlation_id=UUID("11111111-1111-4111-8111-111111111111"),
        task_type="research",
        judge_model="fixture-judge",
        judge_model_version="2026-06-20",
        judge_provider="fixture",
        rubric_id="delegation_non_verifiable_v1",
        rubric_hash=str(_base_event_kwargs()["rubric_hash"]),
        prompt="prompt",
        judged_input="input",
        temperature=0.0,
        judge_node_version="delegation-judge-verdict/1.0.0",
        reasoning="reasoning",
        verdict=EnumDelegationJudgeVerdict.PASS,
        actual_score=0.91,
    )
    assert replay.event_hash == event.event_hash


@pytest.mark.unit
def test_judge_failure_is_typed_event_not_zero_score() -> None:
    event = build_delegation_judge_verdict_event(
        correlation_id=uuid4(),
        task_type="research",
        judge_model="fixture-judge",
        judge_model_version="2026-06-20",
        judge_provider="fixture",
        rubric_id="delegation_non_verifiable_v1",
        rubric_hash=str(_base_event_kwargs()["rubric_hash"]),
        prompt="prompt",
        judged_input="input",
        temperature=0.0,
        judge_node_version="delegation-judge-verdict/1.0.0",
        reasoning="provider timeout",
        verdict=EnumDelegationJudgeVerdict.JUDGE_FAILED,
        actual_score=None,
        failure_kind="provider_timeout",
        failure_message="judge provider did not return a verdict",
    )

    assert event.verdict is EnumDelegationJudgeVerdict.JUDGE_FAILED
    assert event.actual_score is None
    assert event.failure_kind == "provider_timeout"

    payload = _base_event_kwargs()
    payload.update(
        {
            "verdict": "judge_failed",
            "actual_score": 0.0,
            "failure_kind": "provider_timeout",
            "failure_message": "timeout",
        }
    )
    with pytest.raises(ValidationError, match="must not carry a zero score"):
        ModelDelegationJudgeVerdictEvent(**payload)


@pytest.mark.unit
def test_calibration_corpus_declares_expected_ordering_and_marker_controls() -> None:
    corpus = yaml.safe_load(_CORPUS_PATH.read_text())
    rows = {case["id"]: case for case in corpus["cases"]}

    assert (
        rows["research_known_good"]["expected_score"]
        > rows["summary_borderline"]["expected_score"]
    )
    assert (
        rows["summary_borderline"]["expected_score"]
        > rows["research_known_bad"]["expected_score"]
    )
    assert (
        rows["summary_marker_light_correct"]["expected_score"]
        > rows["document_marker_rich_wrong"]["expected_score"]
    )
    assert rows["document_marker_rich_wrong"]["expected_verdict"] == "fail"
    assert rows["summary_marker_light_correct"]["expected_verdict"] == "pass"

    for case in corpus["cases"]:
        event = build_delegation_judge_verdict_event(
            correlation_id=uuid4(),
            task_type=str(case["task_type"]),
            judge_model=str(corpus["judge_model"]),
            judge_model_version=str(corpus["judge_model_version"]),
            judge_provider=str(corpus["judge_provider"]),
            rubric_id=str(corpus["rubric_id"]),
            rubric_hash=str(corpus["rubric_hash"]),
            prompt=str(case["prompt"]),
            judged_input=str(case["input"]) + "\n" + str(case["response"]),
            temperature=float(corpus["temperature"]),
            judge_node_version=str(corpus["judge_node_version"]),
            reasoning=str(case["reasoning"]),
            verdict=EnumDelegationJudgeVerdict(str(case["expected_verdict"])),
            actual_score=float(case["expected_score"]),
        )
        assert event.event_hash == event.compute_event_hash()


@pytest.mark.unit
def test_contracts_declare_judge_verdict_event_and_non_verifiable_authority() -> None:
    rubric = yaml.safe_load(_RUBRIC_PATH.read_text())
    task_contract = yaml.safe_load(_TASK_CONTRACT_PATH.read_text())
    qg_contract = yaml.safe_load(_QUALITY_GATE_CONTRACT_PATH.read_text())

    rubric_def = rubric["rubrics"]["delegation_non_verifiable_v1"]
    assert rubric_def["score_source"] == "reproducible_judge"

    topic = "onex.evt.omnibase-infra.delegation-judge-verdict.v1"
    assert topic in qg_contract["event_bus"]["publish_topics"]
    event_decl = next(
        e
        for e in qg_contract["published_events"]
        if e["event_type"] == "DelegationJudgeVerdictEvent"
    )
    assert event_decl["topic"] == topic
    assert (
        set(qg_contract["metadata"]["judge_verdict_event"]["identity_bundle_fields"])
        >= _IDENTITY_FIELDS
    )
    assert "deterministic judge" not in qg_contract["description"].lower()

    for task_class in rubric_def["applies_to_task_classes"]:
        authority = task_contract["task_classes"][task_class]["adequacy_authority"]
        assert authority["score_source"] == "reproducible_judge"
        assert authority["rubric_id"] == "delegation_non_verifiable_v1"
        assert authority["rubric_hash"] == rubric_def["rubric_hash"]
        assert authority["event_topic_ref"] == "delegation_judge_verdict_v1"


@pytest.mark.unit
def test_projection_contract_and_migration_materialize_judge_verdict_rows() -> None:
    projection = yaml.safe_load(_PROJECTION_CONTRACT_PATH.read_text())
    topic = "onex.evt.omnibase-infra.delegation-judge-verdict.v1"
    snapshot = "onex.snapshot.projection.delegation.judge-verdicts.v1"

    assert topic in projection["event_bus"]["subscribe_topics"]
    assert snapshot in projection["event_bus"]["publish_topics"]
    assert any(
        table["name"] == JUDGE_VERDICT_TABLE
        and table["migration"] == "0016_delegation_judge_verdict_events.sql"
        and table["role"] == "judge_verdict_events"
        for table in projection["db_io"]["db_tables"]
    )

    exposure = next(
        item
        for item in projection["projection_api"]["exposures"]
        if item["topic"] == snapshot
    )
    assert exposure["table"] == JUDGE_VERDICT_TABLE

    migration = _MIGRATION_PATH.read_text()
    assert "CREATE TABLE IF NOT EXISTS delegation_judge_verdict_events" in migration
    assert "actual_score IS NULL" in migration
    assert "event_hash TEXT NOT NULL UNIQUE" in migration


@pytest.mark.unit
def test_sync_projection_writes_judge_verdict_row() -> None:
    event = build_delegation_judge_verdict_event(
        correlation_id=UUID("22222222-2222-4222-8222-222222222222"),
        task_type="summarization",
        judge_model="fixture-judge",
        judge_model_version="2026-06-20",
        judge_provider="fixture",
        rubric_id="delegation_non_verifiable_v1",
        rubric_hash=str(_base_event_kwargs()["rubric_hash"]),
        prompt="summarize",
        judged_input="source and response",
        temperature=0.0,
        judge_node_version="delegation-judge-verdict/1.0.0",
        reasoning="accurate and complete",
        verdict=EnumDelegationJudgeVerdict.PASS,
        actual_score=0.88,
    )
    db = InmemoryDatabaseAdapter()
    payload = event.model_dump(mode="json")
    payload["_db"] = db
    payload["_event_type"] = "onex.evt.omnibase-infra.delegation-judge-verdict.v1"

    result = HandlerProjectionDelegation().handle(payload)

    assert result == {"rows_upserted": 1, "table": JUDGE_VERDICT_TABLE}
    row = db.query(JUDGE_VERDICT_TABLE, {"event_hash": event.event_hash})[0]
    assert row["correlation_id"] == str(event.correlation_id)
    assert row["score_source"] == "reproducible_judge"
    assert row["actual_score"] == pytest.approx(0.88)
    assert row["verdict"] == "pass"
