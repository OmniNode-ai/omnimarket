# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A committed judged run replays as one calibrated event per counted item, or not at all."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_publish_status import (
    EnumAcceptancePublishStatus,
)
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_replay_refusal import (
    EnumAcceptanceReplayRefusal,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_replay_request import (
    ModelAcceptanceReplayRequest,
)
from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
    judged_acceptance_event_id_for,
)
from omnimarket.nodes.node_delegation_acceptance_judged_replay_compute.handlers.handler_delegation_acceptance_judged_replay import (
    HandlerDelegationAcceptanceJudgedReplay,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TIERS = {"Qwen3.8-27B": "local", "glm-5.3": "cheap_cloud"}


def _request(**over: Any) -> ModelAcceptanceReplayRequest:
    rows = [
        json.loads(line)
        for line in (FIXTURES / "judgments_sample.jsonl").read_text().splitlines()
    ]
    body: dict[str, Any] = {
        "rows": rows,
        "summary": json.loads((FIXTURES / "summary_sample.json").read_text()),
        "tenant_id": "820272f9-4aaf-5add-a2df-0af942852ab2",
        "tier_by_model": TIERS,
        "judge_run_id": "2026-10-03-judged-acceptance-matrix",
        "judge_model": "synthetic-judge",
        "judge_model_version": "high-effort",
        "rubric_id": "delegation-acceptance-judge",
        "rubric_version": "v1",
        "rubric_hash": "sha256:" + "b" * 64,
        "calibration_run_id": "synthetic-calibration-76",
        "judged_at": "2026-10-03T18:48:00+00:00",
    }
    body.update(over)
    return ModelAcceptanceReplayRequest.model_validate(body)


def test_replay_one_event_per_counted_item_and_none_for_unjudged_trials() -> None:
    result = HandlerDelegationAcceptanceJudgedReplay().handle(_request())
    assert result.status is EnumAcceptancePublishStatus.COMPLETED
    assert len(result.events) == 4
    assert result.skipped_unjudged_count == 2
    assert {e.kind for e in result.events} == {"task", "edit_loop_turn"}
    assert {(e.delegated_model_key, e.delegated_tier) for e in result.events} == {
        ("Qwen3.8-27B", "local"),
        ("glm-5.3", "cheap_cloud"),
    }


def test_replay_event_carries_the_primary_judges_calibration_and_the_calls_own_time() -> (
    None
):
    request = _request()
    result = HandlerDelegationAcceptanceJudgedReplay().handle(request)
    first = result.events[0]
    row = request.rows[0]
    assert first.call_time == row.timestamp
    assert first.correlation_id == row.correlation_id
    assert first.judged_at == request.judged_at
    assert (first.calibration_n, first.calibration_kappa) == (76, 0.6085164835164836)
    assert first.calibration_agreement == 0.8026315789473685
    assert row.judge_codex is not None
    assert (first.accept, first.quality) == (
        row.judge_codex.accept,
        row.judge_codex.quality,
    )
    assert first.event_id == judged_acceptance_event_id_for(
        request.judge_run_id, row.correlation_id
    )


def test_replay_events_deterministic_byte_identical() -> None:
    handler = HandlerDelegationAcceptanceJudgedReplay()
    one = handler.handle(_request()).model_dump_json()
    two = handler.handle(_request()).model_dump_json()
    assert one == two


def test_replay_refuses_a_model_with_no_tier() -> None:
    result = HandlerDelegationAcceptanceJudgedReplay().handle(
        _request(tier_by_model={"Qwen3.8-27B": "local"})
    )
    assert result.status is EnumAcceptancePublishStatus.FAILED
    assert result.events == ()
    codes = {i.code for i in result.issues}
    assert codes == {EnumAcceptanceReplayRefusal.NO_TIER_FOR_MODEL}
    assert "glm-5.3" in result.issues[0].message


def test_replay_refuses_an_item_with_no_call_time() -> None:
    request = _request()
    rows = [r.model_dump(mode="json") for r in request.rows]
    rows[0]["timestamp"] = None
    result = HandlerDelegationAcceptanceJudgedReplay().handle(_request(rows=rows))
    assert [i.code for i in result.issues] == [EnumAcceptanceReplayRefusal.NO_CALL_TIME]


def test_replay_refuses_a_primary_judge_with_no_verdict() -> None:
    rows = [r.model_dump(mode="json") for r in _request().rows]
    rows[0]["judge_codex"] = None
    result = HandlerDelegationAcceptanceJudgedReplay().handle(_request(rows=rows))
    assert [i.code for i in result.issues] == [
        EnumAcceptanceReplayRefusal.NO_PRIMARY_VERDICT
    ]


def test_replay_refuses_a_count_the_summary_does_not_hold() -> None:
    summary = json.loads((FIXTURES / "summary_sample.json").read_text())
    summary["matrix"] = [{"n": 5}]
    result = HandlerDelegationAcceptanceJudgedReplay().handle(_request(summary=summary))
    assert [i.code for i in result.issues] == [
        EnumAcceptanceReplayRefusal.COUNT_DISAGREES_WITH_SUMMARY
    ]
    assert "4" in result.issues[0].message
    assert "5" in result.issues[0].message


def test_replay_refuses_a_delegated_call_judged_twice() -> None:
    rows = [r.model_dump(mode="json") for r in _request().rows]
    rows[1]["correlation_id"] = rows[0]["correlation_id"]
    result = HandlerDelegationAcceptanceJudgedReplay().handle(_request(rows=rows))
    assert [i.code for i in result.issues] == [
        EnumAcceptanceReplayRefusal.DUPLICATE_CORRELATION_ID
    ]


def test_replay_refuses_a_primary_judge_with_no_calibration() -> None:
    summary = json.loads((FIXTURES / "summary_sample.json").read_text())
    del summary["calibration"]["codex"]
    result = HandlerDelegationAcceptanceJudgedReplay().handle(_request(summary=summary))
    assert {i.code for i in result.issues} == {
        EnumAcceptanceReplayRefusal.NO_CALIBRATION_FOR_PRIMARY
    }


def test_replay_tenant_is_the_requests() -> None:
    result = HandlerDelegationAcceptanceJudgedReplay().handle(_request())
    assert {e.tenant_id for e in result.events} == {
        UUID("820272f9-4aaf-5add-a2df-0af942852ab2")
    }


def test_replay_route_resolves_to_the_typed_handler() -> None:
    from importlib import import_module

    from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route

    route = _resolve_node_route("node_delegation_acceptance_judged_replay_compute")
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationAcceptanceJudgedReplay
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelAcceptanceReplayRequest
    )


def test_replay_contract_declares_its_command_and_built_topics() -> None:
    import yaml

    nodes = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
    contract = yaml.safe_load(
        (
            nodes / "node_delegation_acceptance_judged_replay_compute" / "contract.yaml"
        ).read_text()
    )
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.delegation-acceptance-replay-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.delegation-acceptance-replay-built.v1"
    ]
