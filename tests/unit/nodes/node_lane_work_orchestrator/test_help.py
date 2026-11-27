# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The help step: first responder, pinned requests, answer validation and receipts (OMN-26722)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from omnibase_core.models.lane_work import ModelLaneHelpTier

from omnimarket.nodes.node_lane_work_orchestrator.handlers.handler_lane_work_help import (
    HandlerLaneWorkHelp,
    correlation_for,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_help_receipt import (
    ModelLaneHelpReceipt,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_help_io import (
    ModelLaneWorkHelpAnswerInput,
)

from .conftest import (
    NODE,
    TIER_NAMES,
    build_request,
    driver_in_help,
    example_chain,
)

pytestmark = pytest.mark.unit

HANDLER = HandlerLaneWorkHelp()


def _answer(driver: Any, body: str, **fields: Any) -> Any:
    request = build_request(driver)
    tier = driver.state.help_tiers[driver.state.help_tier_index]
    dispatch = HANDLER.build(request)
    assert dispatch.delegate_request is not None
    values = {
        "state": driver.state,
        "correlation_id": str(dispatch.delegate_request.correlation_id),
        "answer_text": body,
        "executed_backend_id": tier.backend_id,
        "executed_model": tier.model,
    }
    values.update(fields)
    return HANDLER.validate_answer(ModelLaneWorkHelpAnswerInput(**values))


def test_example_chain_is_the_rulings_eight_tiers_in_order() -> None:
    assert tuple(tier.name for tier in example_chain()) == TIER_NAMES


def test_example_files_name_no_gemini_tier() -> None:
    for name in ("overlay.example.yaml", "contract.yaml"):
        assert "gemini" not in (NODE / name).read_text().lower()


@pytest.mark.parametrize("index", range(8))
def test_first_responder_is_the_tier_above_the_worker(index: int) -> None:
    worker = TIER_NAMES[index]
    driver = driver_in_help(worker)
    dispatch = HANDLER.build(build_request(driver))
    if worker == "claude-large":
        assert dispatch.delegate_request is None
        assert dispatch.fold_event is not None
        assert dispatch.fold_event.kind == "help_failed"
        driver.send(
            "help_failed",
            action_id=dispatch.fold_event.action_id,
            decision_needed=dispatch.fold_event.decision_needed,
            reason=dispatch.fold_event.reason,
        )
        assert driver.name == "blocked"
        assert driver.results[-1].intents[-1].inputs["decision"] == (
            "why does the push guard refuse?"
        )
    else:
        assert dispatch.help_request.tier.name == TIER_NAMES[index + 1]
        assert dispatch.delegate_request is not None
        assert (
            dispatch.delegate_request.backend_id
            == example_chain()[index + 1].backend_id
        )


def test_sonnet_worker_asks_claude_large_and_its_failure_ends_blocked() -> None:
    driver = driver_in_help("claude-medium")
    dispatch = HANDLER.build(build_request(driver))
    assert dispatch.delegate_request is not None
    assert dispatch.delegate_request.backend_id == "harness-claude-code-large"
    driver.send("help_failed", action_id=build_request(driver).intent.action_id)
    assert driver.name == "blocked"


def test_every_request_is_pinned_and_waits_for_nothing() -> None:
    for worker in TIER_NAMES[:-1]:
        driver = driver_in_help(worker)
        request = HANDLER.build(build_request(driver)).delegate_request
        assert request is not None
        assert (
            request.backend_id
            == driver.state.help_tiers[driver.state.help_tier_index].backend_id
        )
        assert request.wait is False
        assert request.metadata["effort"]


def test_tier_entries_name_harness_model_and_effort() -> None:
    chain = {tier.name: tier for tier in example_chain()}
    assert {chain[n].harness for n in ("glm-small", "glm-medium", "glm-large")} == {
        "claude-glm"
    }
    assert chain["codex-cheapest"].harness == "codex"
    assert chain["codex-cheapest"].model
    assert chain["codex-cheapest"].effort
    assert {
        chain[n].harness for n in ("claude-small", "claude-medium", "claude-large")
    } == {"claude-code"}


def test_the_climb_takes_the_next_tier_on_help_failed() -> None:
    driver = driver_in_help("local")
    first = HANDLER.build(build_request(driver))
    driver.send("help_failed", action_id=build_request(driver).intent.action_id)
    assert driver.name == "help_requested"
    second = HANDLER.build(build_request(driver))
    assert first.delegate_request is not None
    assert second.delegate_request is not None
    assert first.help_request.tier.name == "glm-small"
    assert second.help_request.tier.name == "glm-medium"
    assert second.delegate_request.backend_id == "harness-claude-glm-medium"


def test_help_request_is_recorded_as_a_ledger_message() -> None:
    dispatch = HANDLER.build(build_request(driver_in_help("claude-small")))
    assert "claude-small asks claude-medium" in dispatch.ledger_message


def test_a_valid_answer_becomes_a_typed_event_with_a_full_receipt() -> None:
    driver = driver_in_help("claude-small")
    body = json.dumps(
        {"kind": "help_cause_found", "cause": "the guard needs a ledger cell"}
    )
    result = _answer(driver, body)
    assert result.fold_event is not None
    assert result.fold_event.kind == "help_cause_found"
    receipt = result.receipt
    assert (receipt.asking_tier, receipt.answering_tier) == (
        "claude-small",
        "claude-medium",
    )
    assert (receipt.executed_backend_id, receipt.executed_model) == (
        "harness-claude-code-medium",
        "sonnet",
    )
    assert receipt.configured_effort == "high"
    assert receipt.outcome == "help_cause_found"
    driver.send(
        result.fold_event.kind,
        action_id=result.fold_event.action_id,
        evidence=result.fold_event.evidence,
    )
    assert driver.name == "fixing"


def test_a_fenced_answer_parses() -> None:
    body = '```json\n{"kind": "help_cause_found", "cause": "c"}\n```'
    assert _answer(driver_in_help("local"), body).receipt.outcome == "help_cause_found"


def test_an_answer_from_another_run_is_refused() -> None:
    driver = driver_in_help("local")
    body = json.dumps({"kind": "help_cause_found", "cause": "c"})
    result = _answer(
        driver, body, correlation_id=str(correlation_for("some-other-run"))
    )
    assert result.fold_event is None
    assert result.receipt.outcome == "refused"


def test_an_answer_that_does_not_parse_is_help_failed() -> None:
    result = _answer(driver_in_help("local"), "I think it is the guard")
    assert result.fold_event is not None
    assert result.fold_event.kind == "help_failed"
    assert result.receipt.outcome == "help_failed"


@pytest.mark.parametrize(
    "fields",
    [
        {"executed_model": "another-model"},
        {"executed_backend_id": "another-backend"},
        {"executed_backend_id": None, "executed_model": None},
    ],
)
def test_a_run_other_than_the_tiers_backend_and_model_is_help_failed(
    fields: dict[str, str | None],
) -> None:
    body = json.dumps({"kind": "help_cause_found", "cause": "c"})
    result = _answer(driver_in_help("local"), body, **fields)
    assert result.fold_event is not None
    assert result.fold_event.kind == "help_failed"
    assert result.receipt.outcome == "help_failed"


def test_a_failed_delegation_is_help_failed() -> None:
    result = _answer(driver_in_help("local"), "", failed=True)
    assert result.fold_event is not None
    assert result.fold_event.kind == "help_failed"


def test_help_retry_to_a_destination_the_contract_does_not_allow_climbs() -> None:
    driver = driver_in_help("local")
    body = json.dumps({"kind": "help_retry", "help_origin": "diagnosing"})
    result = _answer(driver, body)
    assert result.fold_event is not None
    assert result.fold_event.kind == "help_retry"
    # the handler passes it on; the reducer has no such edge from help_requested, so it counts as help_failed
    driver.send(
        result.fold_event.kind,
        action_id=result.fold_event.action_id,
        help_origin=result.fold_event.help_origin,
    )
    assert driver.name == "help_requested"
    assert driver.state.help_tier_index == 2


def test_receipt_model_carries_asking_answering_executed_effort_and_outcome() -> None:
    names = set(ModelLaneHelpReceipt.model_fields)
    assert {
        "asking_tier",
        "answering_tier",
        "executed_backend_id",
        "executed_model",
        "configured_effort",
        "outcome",
    } <= names
    bad: dict[str, Any] = {
        "workflow_id": "w",
        "action_id": "a",
        "asking_tier": "x",
        "answering_tier": "y",
        "configured_effort": "high",
        "outcome": "bogus",
    }
    with pytest.raises(ValueError, match="outcome"):
        ModelLaneHelpReceipt(**bad)


def test_chain_tier_model_is_the_core_one() -> None:
    assert all(isinstance(tier, ModelLaneHelpTier) for tier in example_chain())
