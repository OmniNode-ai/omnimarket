# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test (OMN-24533): the Fable review gate, the hostile reviewer start command and the completed event's receipt.

The three pieces meet in the review the orchestrator runs: the start command names route keys, the gate decides from them and
from the host the review runs on, and the completed event carries the gate's verdict as ``fable_gate``. Each test below fails
when either side changes shape (the shape lock), when the orchestrator stops asking the gate (the skipped-gate control), or
when the receipt is dropped from the event (the dropped-receipt control). The review runs on the in-memory bus the way the
runtime hands a decoded command to the handler and publishes what it returns; the real broker was not used (the lane rule is
never to touch the broker).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.events.fable_review_gate import (
    EnumFableReviewRefusal,
    ModelFableReviewGateOverlay,
    ModelFableReviewGateVerdict,
    ModelFableReviewReceipt,
)
from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter
from omnimarket.nodes.node_hostile_reviewer_orchestrator.handlers.handler_hostile_reviewer_orchestrator import (
    HandlerHostileReviewerOrchestrator,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_completed_event import (
    ModelHostileReviewerCompletedEvent,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_phase import (
    EnumHostileReviewerPhase,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

pytestmark = pytest.mark.unit

START_TOPIC = "onex.cmd.omnimarket.hostile-reviewer-start.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.hostile-reviewer-completed.v1"
DIFF = "diff --git a/demo/fictional.py b/demo/fictional.py\n+def pay(amount):\n+    return amount\n"
OVERLAY = ModelFableReviewGateOverlay(
    fable_route_keys=("synthetic-fable",),
    orchestrating_hosts=("synthetic-mac",),
    host_name_env="SYNTHETIC_HOST_NAME",
)
MAC = {"SYNTHETIC_HOST_NAME": "synthetic-mac"}
LAB = {"SYNTHETIC_HOST_NAME": "synthetic-lab"}


class _CountingAdapter(ModelInferenceAdapter):
    """Counts the model calls and answers no findings; it is the counter for 'no model call', not a fake of real output."""

    def __init__(self) -> None:
        self.calls = 0

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        self.calls += 1
        return "[]"


class _SkipGateHandler(HandlerHostileReviewerOrchestrator):
    """Injected break: the orchestrator no longer asks the gate and admits every review."""

    def _fable_gate_verdict(
        self, command: ModelHostileReviewerStartCommand, decided_at: datetime
    ) -> ModelFableReviewGateVerdict | None:
        real = super()._fable_gate_verdict(command, decided_at)
        assert real is not None
        return real.model_copy(update={"admitted": True, "refusal": None})


class _DropReceiptHandler(HandlerHostileReviewerOrchestrator):
    """Injected break: the completed event leaves the orchestrator without the gate's receipt."""

    async def _run_review(
        self, command: ModelHostileReviewerStartCommand, started_at: datetime
    ) -> ModelHostileReviewerCompletedEvent:
        event = await super()._run_review(command, started_at)
        return event.model_copy(update={"fable_gate": None})


def _command(models: list[str]) -> ModelHostileReviewerStartCommand:
    return ModelHostileReviewerStartCommand(
        correlation_id=uuid4(),
        diff_content=DIFF,
        models=models,
        dry_run=False,
        requested_at=datetime.now(tz=UTC),
    )


def _handler(
    cls: type[HandlerHostileReviewerOrchestrator],
    adapter: _CountingAdapter,
    environ: dict[str, str],
) -> HandlerHostileReviewerOrchestrator:
    return cls(inference_adapter=adapter, fable_overlay=OVERLAY, host_environ=environ)


async def _review(
    cls: type[HandlerHostileReviewerOrchestrator],
    environ: dict[str, str],
    models: list[str],
) -> tuple[
    ModelHostileReviewerCompletedEvent,
    ModelHostileReviewerStartCommand,
    _CountingAdapter,
]:
    adapter = _CountingAdapter()
    command = _command(models)
    event = await _handler(cls, adapter, environ).handle(command)
    return event, command, adapter


async def _lab_origin_is_refused(cls: type[HandlerHostileReviewerOrchestrator]) -> None:
    event, _, adapter = await _review(cls, LAB, ["synthetic-fable"])
    assert event.final_phase == EnumHostileReviewerPhase.FAILED
    assert event.fable_gate is not None
    assert (
        event.fable_gate.refusal is EnumFableReviewRefusal.ORIGIN_NOT_ORCHESTRATING_MAC
    )
    assert adapter.calls == 0


async def _mac_origin_receipt_is_carried(
    cls: type[HandlerHostileReviewerOrchestrator],
) -> None:
    event, command, adapter = await _review(cls, MAC, ["synthetic-fable"])
    assert event.fable_gate is not None
    receipt = event.fable_gate.receipt
    assert event.fable_gate.admitted is True
    assert receipt.origin_host == "synthetic-mac"
    assert receipt.matched_orchestrating_host == "synthetic-mac"
    assert receipt.correlation_id == command.correlation_id
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_join_golden_mac_origin_receipt() -> None:
    await _mac_origin_receipt_is_carried(HandlerHostileReviewerOrchestrator)


@pytest.mark.asyncio
async def test_join_refused_lab_origin_no_model_call() -> None:
    event, command, adapter = await _review(
        HandlerHostileReviewerOrchestrator, LAB, ["synthetic-fable"]
    )
    assert event.final_phase == EnumHostileReviewerPhase.FAILED
    assert event.correlation_id == command.correlation_id
    assert event.fable_gate is not None
    assert (
        event.fable_gate.refusal is EnumFableReviewRefusal.ORIGIN_NOT_ORCHESTRATING_MAC
    )
    assert event.fable_gate.receipt.origin_host == "synthetic-lab"
    assert EnumFableReviewRefusal.ORIGIN_NOT_ORCHESTRATING_MAC.value in (
        event.error_message or ""
    )
    assert event.models_failed == ["synthetic-fable"]
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_join_unreadable_origin_refused() -> None:
    event, _, adapter = await _review(
        HandlerHostileReviewerOrchestrator, {}, ["synthetic-fable"]
    )
    assert event.final_phase == EnumHostileReviewerPhase.FAILED
    assert event.fable_gate is not None
    assert event.fable_gate.refusal is EnumFableReviewRefusal.ORIGIN_UNREADABLE
    assert EnumFableReviewRefusal.ORIGIN_UNREADABLE.value in (event.error_message or "")
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_join_non_fable_route_unchanged() -> None:
    event, _, adapter = await _review(
        HandlerHostileReviewerOrchestrator, LAB, ["synthetic-other"]
    )
    assert event.final_phase == EnumHostileReviewerPhase.DONE
    assert event.fable_gate is not None
    assert event.fable_gate.applies is False
    assert event.fable_gate.admitted is True
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_join_no_overlay_gate_not_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", "")
    adapter = _CountingAdapter()
    event = await HandlerHostileReviewerOrchestrator(inference_adapter=adapter).handle(
        _command(["synthetic-fable"])
    )
    assert event.final_phase == EnumHostileReviewerPhase.DONE
    assert event.fable_gate is None
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_join_receipt_wire_round_trip() -> None:
    for environ in (MAC, LAB):
        event, _, _ = await _review(
            HandlerHostileReviewerOrchestrator, environ, ["synthetic-fable"]
        )
        again = ModelHostileReviewerCompletedEvent.model_validate_json(
            event.model_dump_json()
        )
        assert again.fable_gate == event.fable_gate
        assert again.fable_gate is not None


def test_join_shape_lock() -> None:
    assert set(ModelFableReviewGateVerdict.model_fields) == {
        "admitted",
        "applies",
        "refusal",
        "receipt",
    }
    assert set(ModelFableReviewReceipt.model_fields) == {
        "correlation_id",
        "origin_host",
        "matched_orchestrating_host",
        "fable_route_keys",
        "decided_at",
    }
    annotation = ModelHostileReviewerCompletedEvent.model_fields[
        "fable_gate"
    ].annotation
    assert annotation == ModelFableReviewGateVerdict | None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_join_on_the_in_memory_bus_the_lab_origin_reads_its_refusal(
    integration_event_bus: Any,
) -> None:
    """The runtime's entry shape: the decoded start command goes to the handler, what it returns goes on the terminal topic."""
    adapter = _CountingAdapter()
    inner = _handler(HandlerHostileReviewerOrchestrator, adapter, LAB)

    class _Wrapper:
        async def handle(self, **payload: Any) -> Any:
            return await inner.handle(ModelHostileReviewerStartCommand(**payload))

    await integration_event_bus.start()
    try:
        runtime = LocalRuntimeBusAdapter(
            handler=_Wrapper(),
            handler_name="hostile-reviewer-orchestrator",
            input_model_cls=ModelHostileReviewerStartCommand,
            output_topic=COMPLETED_TOPIC,
            bus=integration_event_bus,
        )
        await integration_event_bus.subscribe(
            START_TOPIC, on_message=runtime.on_message, group_id="fable-gate-join"
        )
        command = _command(["synthetic-fable"])
        await integration_event_bus.publish(
            START_TOPIC, key=None, value=command.model_dump_json().encode("utf-8")
        )
        history = await integration_event_bus.get_event_history(topic=COMPLETED_TOPIC)
        assert len(history) == 1
        payload = json.loads(history[0].value)
        assert payload["correlation_id"] == str(command.correlation_id)
        assert payload["final_phase"] == "failed"
        assert payload["fable_gate"]["refusal"] == "origin_not_orchestrating_mac"
        assert payload["fable_gate"]["receipt"]["origin_host"] == "synthetic-lab"
        assert adapter.calls == 0
    finally:
        await integration_event_bus.close()


@pytest.mark.asyncio
async def test_join_control_skipped_gate_reads_broken() -> None:
    await _lab_origin_is_refused(HandlerHostileReviewerOrchestrator)
    with pytest.raises(AssertionError):
        await _lab_origin_is_refused(_SkipGateHandler)


@pytest.mark.asyncio
async def test_join_control_dropped_receipt_reads_broken() -> None:
    await _mac_origin_receipt_is_carried(HandlerHostileReviewerOrchestrator)
    with pytest.raises(AssertionError):
        await _mac_origin_receipt_is_carried(_DropReceiptHandler)
