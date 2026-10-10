# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shadow non-interference and sample-gated calibration through the node seam."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml

from omnimarket.inference.task_class_authority import (
    EnumTaskTypeResolution,
    load_task_class_authority,
)
from omnimarket.nodes.node_typed_decision_effect.handlers.handler_typed_decision import (
    HandlerTypedDecisionWorkflow,
)
from omnimarket.nodes.node_typed_decision_effect.models.model_typed_decision import (
    EnumTypedDecisionDecider,
    ModelTypedDecisionCalibrationObservation,
    ModelTypedDecisionCalibrationReport,
    ModelTypedDecisionCalibrationRequest,
    ModelTypedDecisionRequest,
    ModelTypedDecisionWorkflowRequest,
)
from tests.unit.nodes.node_typed_decision_effect.test_handler_typed_decision import (
    _BACKEND_ID,
    _NODE_DIR,
    _choice,
    _choice_answer,
    _handler,
    _Recorder,
)

pytestmark = pytest.mark.unit


def test_existing_shape_gate_and_all_fifteen_captured_prompts() -> None:
    authority = load_task_class_authority()
    captured = "Summarize in one sentence: the broker accepted this request."
    assert (
        authority.resolve_task_type(captured, explicit=None).task_type
        == "summarization"
    )
    control = (
        "Implement a function that adds integers. "
        + "Return working Python code. " * 40
    )
    assert (
        authority.resolve_task_type(control, explicit=None).task_type != "summarization"
    )
    corpus = (
        _NODE_DIR.parents[3] / "tests/fixtures/delegation/omn19140/shadow_corpus.yaml"
    )
    rows = yaml.safe_load(corpus.read_text())["rows"]
    floor_rows = [
        row
        for row in rows
        if row["shadow_label"] == "summarization"
        and row["incumbent_before"] != "summarization"
    ]
    assert len(floor_rows) == 15
    for row in floor_rows:
        result = authority.resolve_task_type(row["prompt"], explicit=None)
        assert result.task_type == "summarization", row["row"]
        assert result.resolution is EnumTaskTypeResolution.CONTRACT, row["row"]


@pytest.mark.parametrize(
    ("decision", "key", "shadow_decider", "reason"),
    [
        (_choice_answer("change", 0.95), True, "model", None),
        (
            _choice_answer("change", 0.75),
            True,
            "incumbent_abstained",
            "below_abstention_threshold",
        ),
        (
            _choice_answer("change", 0.95),
            False,
            "incumbent_refused",
            "credential_not_registered",
        ),
        (
            httpx.ReadTimeout("slow"),
            True,
            "incumbent_backend_error",
            "backend_transport_error",
        ),
        (
            httpx.Response(503, json={"detail": "unavailable"}),
            True,
            "incumbent_backend_error",
            "backend_http_error",
        ),
        (
            _choice_answer("change", 1.1),
            True,
            "incumbent_backend_error",
            "backend_malformed_response",
        ),
    ],
)
def test_shadow_always_returns_incumbent_and_logs_actual_backend_outcome(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    decision: httpx.Response | Exception,
    key: bool,
    shadow_decider: str,
    reason: str | None,
) -> None:
    recorder = _Recorder(decision)
    handler = HandlerTypedDecisionWorkflow(
        decision_handler=_handler(
            recorder, key="test-key" if key else None, tmp=tmp_path
        )
    )
    request = _choice()
    with caplog.at_level("INFO"):
        result = handler.handle(ModelTypedDecisionWorkflowRequest(decision=request))
    receipt = result.decision
    assert receipt is not None
    assert receipt.answer is not None
    assert request.incumbent_answer is not None
    assert receipt.answer.encode() == request.incumbent_answer.encode()
    assert receipt.decided_by is EnumTypedDecisionDecider.INCUMBENT_SHADOW
    assert receipt.shadow_decided_by == shadow_decider
    assert receipt.reason == reason
    assert receipt.correlation_id == request.correlation_id
    assert any(
        record.decision_receipt == receipt.model_dump(mode="json")
        for record in caplog.records
        if hasattr(record, "decision_receipt")
    )
    assert "test-key" not in caplog.text
    assert len(recorder.decision_calls) == int(key)


def test_shadow_requires_an_incumbent_and_cannot_accept_a_live_arm() -> None:
    with pytest.raises(ValueError, match="incumbent"):
        ModelTypedDecisionWorkflowRequest(decision=_choice(incumbent_answer=None))
    with pytest.raises(ValueError, match="Extra inputs"):
        ModelTypedDecisionWorkflowRequest.model_validate(
            {"decision": _choice().model_dump(), "mode": "live"}
        )


def test_direct_effect_wire_shape_is_preserved(tmp_path: Path) -> None:
    result = _handler(_Recorder(_choice_answer("change", 0.95)), tmp=tmp_path).handle(
        _choice()
    )
    assert result.decided_by is EnumTypedDecisionDecider.MODEL
    assert "shadow_decided_by" not in result.model_dump(mode="json")


def test_task_class_residual_uses_real_incumbent_and_withholds_its_label(
    tmp_path: Path,
) -> None:
    prompt = "Return READY and nothing else."
    incumbent = load_task_class_authority().resolve_task_type(prompt, explicit=None)
    assert incumbent.resolution is EnumTaskTypeResolution.FALLBACK
    response = httpx.Response(
        200,
        json={
            "answers": {
                "decision": {
                    "type": "choice",
                    "choice": "summarization",
                    "probabilities": {"document": 0.05, "summarization": 0.95},
                }
            }
        },
    )
    recorder = _Recorder(response)
    request = ModelTypedDecisionRequest.model_validate(
        {
            **_choice().model_dump(),
            "state": prompt,
            "instructions": "Which task class describes the requested output?",
            "criteria": {
                "document": {
                    "what": "Draft general prose.",
                    "not_for": "Condense supplied material.",
                },
                "summarization": {
                    "what": "Condense supplied material.",
                    "not_for": "An arbitrary prose reply.",
                },
            },
            "incumbent_answer": incumbent.task_type,
        }
    )
    result = HandlerTypedDecisionWorkflow(
        decision_handler=_handler(recorder, tmp=tmp_path)
    ).handle(
        ModelTypedDecisionWorkflowRequest(decision=request),
    )
    assert result.decision is not None
    assert result.decision.answer == incumbent.task_type
    assert result.decision.model_answer == "summarization"
    assert result.decision.shadow_decided_by is EnumTypedDecisionDecider.MODEL
    (call,) = recorder.decision_calls
    body = json.loads(call.content)
    assert body["state"] == prompt
    assert "incumbent_answer" not in body
    assert "task_type" not in body


def _observations(n: int) -> tuple[ModelTypedDecisionCalibrationObservation, ...]:
    return tuple(
        ModelTypedDecisionCalibrationObservation(
            correlation_id=uuid4(),
            probabilities={"document": 0.75, "summarization": 0.25},
            adjudicated_answer="summarization" if index % 4 == 0 else "document",
        )
        for index in range(n)
    )


@pytest.mark.parametrize("n", [0, 22, 299])
def test_calibration_refuses_every_metric_below_three_hundred(n: int) -> None:
    result = HandlerTypedDecisionWorkflow().handle(
        ModelTypedDecisionWorkflowRequest(
            calibration=ModelTypedDecisionCalibrationRequest(
                observations=_observations(n)
            ),
        )
    )
    report = result.calibration
    assert report is not None
    assert report.sample_count == n
    assert report.reason == "insufficient_samples"
    assert report.minimum_samples == 300
    assert report.brier_score is None
    assert report.per_option_ece is None
    assert report.reliability_diagram is None
    for metric, value in (
        ("brier_score", 0.1),
        ("per_option_ece", {}),
        ("reliability_diagram", {}),
    ):
        with pytest.raises(ValueError, match="sample floor"):
            ModelTypedDecisionCalibrationReport.model_validate(
                {**report.model_dump(), metric: value}
            )


def test_calibration_at_boundary_uses_adjudicated_labels_and_fifteen_bins() -> None:
    result = HandlerTypedDecisionWorkflow().handle(
        ModelTypedDecisionWorkflowRequest(
            calibration=ModelTypedDecisionCalibrationRequest(
                observations=_observations(300)
            ),
        )
    )
    report = result.calibration
    assert report is not None
    assert report.reason is None
    assert report.sample_count == 300
    assert report.brier_score == pytest.approx(0.375)
    assert report.per_option_ece == {"document": 0.0, "summarization": 0.0}
    assert report.reliability_diagram is not None
    for option, bins in report.reliability_diagram.items():
        assert len(bins) == 15
        assert sum(cell.count for cell in bins) == 300
        occupied = [cell for cell in bins if cell.count]
        assert len(occupied) == 1
        assert (
            occupied[0].mean_probability
            == {"document": 0.75, "summarization": 0.25}[option]
        )
        assert occupied[0].observed_frequency == occupied[0].mean_probability


def test_duplicate_decisions_and_unadjudicated_or_invalid_data_cannot_fill_the_floor() -> (
    None
):
    observation = _observations(1)[0]
    with pytest.raises(ValueError, match="unique"):
        ModelTypedDecisionCalibrationRequest(observations=(observation,) * 300)
    with pytest.raises(ValueError, match="adjudicated_answer"):
        ModelTypedDecisionCalibrationObservation.model_validate(
            {
                "correlation_id": uuid4(),
                "probabilities": {"a": 0.5, "b": 0.5},
                "incumbent_answer": "a",
            }
        )
    for probabilities in (
        {"a": -0.1, "b": 1.1},
        {"a": 0.5, "b": 0.1},
        {"a": float("nan"), "b": 0.5},
    ):
        with pytest.raises(ValueError, match="probabilities"):
            ModelTypedDecisionCalibrationObservation(
                correlation_id=uuid4(),
                probabilities=probabilities,
                adjudicated_answer="a",
            )


def test_calibration_detects_miscalibration_and_probability_endpoints() -> None:
    observations = tuple(
        ModelTypedDecisionCalibrationObservation(
            correlation_id=uuid4(),
            probabilities={"document": 0.75, "summarization": 0.25},
            adjudicated_answer="document",
        )
        for _ in range(300)
    )
    handler = HandlerTypedDecisionWorkflow()
    result = handler.handle(
        ModelTypedDecisionWorkflowRequest(
            calibration=ModelTypedDecisionCalibrationRequest(observations=observations),
        )
    )
    report = result.calibration
    assert report is not None
    assert report.brier_score == pytest.approx(0.125)
    assert report.per_option_ece == {"document": 0.25, "summarization": 0.25}
    perfect = tuple(
        ModelTypedDecisionCalibrationObservation(
            correlation_id=uuid4(),
            probabilities={"document": 1.0, "summarization": 0.0},
            adjudicated_answer="document",
        )
        for _ in range(300)
    )
    result = handler.handle(
        ModelTypedDecisionWorkflowRequest(
            calibration=ModelTypedDecisionCalibrationRequest(observations=perfect),
        )
    )
    report = result.calibration
    assert report is not None
    assert report.brier_score == 0.0
    assert report.reliability_diagram is not None
    assert report.reliability_diagram["document"][-1].count == 300
    assert report.reliability_diagram["summarization"][0].count == 300


async def test_bus_seam_discovers_the_existing_node_and_returns_a_shadow_receipt(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
    from omnibase_infra.runtime.auto_wiring.discovery import _parse_contract
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )
    from omnibase_infra.runtime.service_dispatch_result_applier import (
        DispatchResultApplier,
    )

    from omnimarket.nodes.node_typed_decision_effect.models.model_typed_decision import (
        ModelTypedDecisionWorkflowResult,
    )

    contract = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.typed-decision-shadow-completed.v1"
    )
    declaration = contract["handler"]
    handler_type = getattr(
        importlib.import_module(declaration["module"]), declaration["class"]
    )
    module, _, name = declaration["input_model"].rpartition(".")
    request_type = getattr(importlib.import_module(module), name)
    recorder = _Recorder(_choice_answer("change", 0.95))
    handler = handler_type(decision_handler=_handler(recorder, tmp=tmp_path))
    request = request_type.model_validate_json(
        ModelTypedDecisionWorkflowRequest(decision=_choice()).model_dump_json()
    )
    discovered = _parse_contract(
        contract_path=_NODE_DIR / "contract.yaml",
        entry_point_name="node_typed_decision_effect",
        package_name="omnimarket",
        package_version="0.0.0",
    )
    assert discovered.handler_routing is not None
    (route,) = discovered.handler_routing.handlers
    assert route.event_model is not None
    callback = _make_dispatch_callback(handler, event_model=route.event_model)
    dispatched = await callback(
        ModelEventEnvelope(
            payload=request.model_dump(mode="json"),
            correlation_id=request.decision.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=request.operation,
            source_tool="shadow-calibration-test",
        )
    )
    assert dispatched is not None
    assert dispatched.correlation_id == request.decision.correlation_id
    assert dispatched.output_count == 1
    (result,) = dispatched.output_events
    assert result.decision.answer == "infrastructure"
    assert result.decision.backend_id == _BACKEND_ID
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    import tomllib

    root = _NODE_DIR.parents[3]
    registry = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]
    assert (
        registry["node_typed_decision_effect"]
        == "omnimarket.nodes.node_typed_decision_effect"
    )
    bus = EventBusInmemory(environment="test", group="shadow-calibration")
    received = []
    applier = DispatchResultApplier(
        event_bus=bus,
        output_topic=contract["terminal_event"],
        allowed_output_topics=contract["event_bus"]["publish_topics"],
    )

    async def on_terminal(message: ModelEventMessage) -> None:
        received.append(
            ModelEventEnvelope[ModelTypedDecisionWorkflowResult].model_validate_json(
                message.value
            )
        )

    async def on_command(message: ModelEventMessage) -> None:
        await applier.apply(
            await callback(
                ModelEventEnvelope[object].model_validate_json(message.value)
            )
        )

    await bus.start()
    try:
        await bus.subscribe(
            contract["terminal_event"], group_id="shadow-result", on_message=on_terminal
        )
        await bus.subscribe(
            contract["runtime_dispatch"]["command_topic"],
            group_id="shadow-command",
            on_message=on_command,
        )
        envelope = ModelEventEnvelope(
            payload=request,
            correlation_id=request.decision.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=request.operation,
            source_tool="shadow-calibration-test",
        )
        await bus.publish(
            contract["runtime_dispatch"]["command_topic"],
            None,
            envelope.model_dump_json().encode(),
            None,
        )
        (terminal,) = received
        assert terminal.correlation_id == request.decision.correlation_id
        assert terminal.payload.decision.answer == "infrastructure"
        assert (
            terminal.payload.decision.decided_by
            is EnumTypedDecisionDecider.INCUMBENT_SHADOW
        )
        assert (
            terminal.payload.decision.shadow_decided_by
            is EnumTypedDecisionDecider.MODEL
        )
    finally:
        await bus.close()
