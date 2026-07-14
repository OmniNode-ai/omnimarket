# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Focused contract + unit tests for node_codegen_outcome_reducer (OMN-14608).

The end-to-end proof that the reducer joins REAL downstream verdicts over the
actual pub/sub seam lives in
``tests/nodes/node_hybrid_codegen_orchestrator/test_golden_chain_composition.py``.
These tests pin the two invariants a reviewer needs to trust that file: (1) the
contract routes all four verdict topics with an explicit per-topic event_model
and ``message_category: event`` (the OMN-14534 / OMN-14605 dispatch trap), and
(2) the join keys on ``correlation_id`` and fails loud on an unseeded verdict.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.codegen.models import (
    ModelCodegenPipelineState,
    ModelCodegenSerializeOutcome,
    ModelCodegenSpec,
    ModelCodegenTypecheckOutcome,
    ModelCodegenValidationOutcome,
    ModelGeneratedCodeValidation,
    ModelLlmGenerateResult,
    ModelMypyCheckResult,
)
from omnimarket.contract_assembly.models import EnumLintStatus, ModelContractDocument
from omnimarket.nodes.node_codegen_outcome_reducer.handlers.handler_codegen_outcome_reducer import (
    HandlerCodegenOutcomeReducer,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_codegen_outcome_reducer"
    / "contract.yaml"
)

# The four raw verdict topics the reducer consumes -> the models the dispatcher
# must validate each into before calling handle() (the OMN-14534 fix pattern).
_EXPECTED_ROUTING: dict[str, str] = {
    "onex.evt.omnimarket.codegen-llm-generated.v1": "ModelLlmGenerateResult",
    "onex.evt.omnimarket.generated-code-validation-completed.v1": (
        "ModelGeneratedCodeValidation"
    ),
    "onex.evt.omnimarket.mypy-check-completed.v1": "ModelMypyCheckResult",
    "onex.evt.omnimarket.contract-serialize-completed.v1": "ModelContractDocument",
}

_EXPECTED_PUBLISH = {
    "onex.evt.omnimarket.codegen-validation-outcome.v1",
    "onex.evt.omnimarket.codegen-typecheck-outcome.v1",
    "onex.evt.omnimarket.codegen-serialize-outcome.v1",
}


def _contract() -> dict[str, object]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


def _state(
    correlation_id: str, source: str = "class X: pass\n"
) -> ModelCodegenPipelineState:
    return ModelCodegenPipelineState(
        spec=ModelCodegenSpec(node_name="NodeX", namespace="ns", archetype="compute"),
        correlation_id=correlation_id,
        source_text=source,
    )


class TestContractRouting:
    """The contract routes every verdict topic to a typed event_model as an event."""

    def test_subscribe_and_publish_topics(self) -> None:
        event_bus = _contract()["event_bus"]
        assert set(event_bus["subscribe_topics"]) == set(_EXPECTED_ROUTING)
        assert set(event_bus["publish_topics"]) == _EXPECTED_PUBLISH

    def test_every_topic_has_typed_event_model_and_event_category(self) -> None:
        routing = _contract()["handler_routing"]
        assert routing["routing_strategy"] == "topic_match"
        by_topic = {entry["topic"]: entry for entry in routing["handlers"]}
        assert set(by_topic) == set(_EXPECTED_ROUTING)
        for topic, expected_model in _EXPECTED_ROUTING.items():
            entry = by_topic[topic]
            # Per-topic event_model so the dispatcher validates the real producer
            # wire shape before calling handle() (never the raw envelope).
            assert entry["event_model"]["name"] == expected_model, topic
            # All four subscribe topics are EVENTS, not commands (the OMN-14605
            # mixed-category NO_DISPATCHER trap).
            assert entry["message_category"] == "event", topic


class TestReducerJoin:
    """The handler joins a raw verdict to retained state on correlation_id."""

    def test_seed_returns_none_and_records_state(self) -> None:
        reducer = HandlerCodegenOutcomeReducer()
        state = _state("corr-1")
        assert reducer.handle(ModelLlmGenerateResult(state=state)) is None

    def test_validation_verdict_joins_retained_state(self) -> None:
        reducer = HandlerCodegenOutcomeReducer()
        reducer.handle(ModelLlmGenerateResult(state=_state("corr-1", source="SRC")))
        outcome = reducer.handle(
            ModelGeneratedCodeValidation(
                parses=True,
                syntax_error=None,
                stub_methods=(),
                structure_issues=(),
                is_valid=True,
                correlation_id="corr-1",
            )
        )
        assert isinstance(outcome, ModelCodegenValidationOutcome)
        assert outcome.is_valid
        assert outcome.state.correlation_id == "corr-1"
        assert outcome.state.source_text == "SRC"  # retained state, not the verdict

    def test_invalid_verdict_flattens_issues(self) -> None:
        reducer = HandlerCodegenOutcomeReducer()
        reducer.handle(ModelLlmGenerateResult(state=_state("corr-1")))
        outcome = reducer.handle(
            ModelGeneratedCodeValidation(
                parses=True,
                syntax_error=None,
                stub_methods=("handle",),
                structure_issues=("bad base",),
                is_valid=False,
                correlation_id="corr-1",
            )
        )
        assert isinstance(outcome, ModelCodegenValidationOutcome)
        assert not outcome.is_valid
        assert outcome.issues == ("stub method: handle", "bad base")

    def test_typecheck_verdict_joins_retained_state(self) -> None:
        reducer = HandlerCodegenOutcomeReducer()
        reducer.handle(ModelLlmGenerateResult(state=_state("corr-1")))
        outcome = reducer.handle(
            ModelMypyCheckResult(
                success=False,
                error_count=3,
                diagnostics=(),
                mypy_available=True,
                correlation_id="corr-1",
            )
        )
        assert isinstance(outcome, ModelCodegenTypecheckOutcome)
        assert not outcome.success
        assert outcome.error_count == 3

    def test_serialize_verdict_advances_state_with_contract(self) -> None:
        reducer = HandlerCodegenOutcomeReducer()
        reducer.handle(ModelLlmGenerateResult(state=_state("corr-1")))
        outcome = reducer.handle(
            ModelContractDocument(
                contract_yaml="name: x\n",
                contract_sha256="abc",
                subcontracts_rendered=(),
                lint_status=EnumLintStatus.PASS,
                correlation_id="corr-1",
            )
        )
        assert isinstance(outcome, ModelCodegenSerializeOutcome)
        assert outcome.state.contract_yaml == "name: x\n"

    def test_two_correlations_do_not_cross(self) -> None:
        reducer = HandlerCodegenOutcomeReducer()
        reducer.handle(ModelLlmGenerateResult(state=_state("corr-A", source="A_SRC")))
        reducer.handle(ModelLlmGenerateResult(state=_state("corr-B", source="B_SRC")))
        outcome = reducer.handle(
            ModelMypyCheckResult(
                success=True,
                error_count=0,
                diagnostics=(),
                mypy_available=True,
                correlation_id="corr-B",
            )
        )
        assert isinstance(outcome, ModelCodegenTypecheckOutcome)
        assert outcome.state.source_text == "B_SRC"  # keyed join, not last-seen

    def test_unseeded_correlation_fails_loud(self) -> None:
        reducer = HandlerCodegenOutcomeReducer()
        with pytest.raises(ValueError, match="no retained pipeline state"):
            reducer.handle(
                ModelMypyCheckResult(
                    success=True,
                    error_count=0,
                    diagnostics=(),
                    mypy_available=True,
                    correlation_id="never-seeded",
                )
            )

    def test_blank_correlation_id_on_seed_fails_loud(self) -> None:
        """A seed whose correlation_id was never propagated fails loud, not silent."""
        reducer = HandlerCodegenOutcomeReducer()
        with pytest.raises(ValueError, match="blank correlation_id"):
            reducer.handle(ModelLlmGenerateResult(state=_state("")))

    def test_blank_correlation_id_on_verdict_fails_loud(self) -> None:
        """A verdict with a blank id must not silently join whatever seed came last."""
        reducer = HandlerCodegenOutcomeReducer()
        reducer.handle(ModelLlmGenerateResult(state=_state("corr-1")))
        with pytest.raises(ValueError, match="blank correlation_id"):
            reducer.handle(
                ModelMypyCheckResult(
                    success=True,
                    error_count=0,
                    diagnostics=(),
                    mypy_available=True,
                    correlation_id="",
                )
            )
