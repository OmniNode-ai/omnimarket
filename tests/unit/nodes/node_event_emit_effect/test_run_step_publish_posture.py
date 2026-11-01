# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21320: a delegation run-step publish resolves a posture and is delivered.

The delegate-skill run-step recorder publishes each step with event_type
``delegation.run_step`` and a topic override, the topic the orchestrator
contract declares as ``runtime_dispatch.run_step_event``. Neither was in the
event registry, so ``node_event_emit_effect`` refused every step (OMN-17237,
fail closed) and the run-trace projection received none over the bus.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.run_step_recorder import (
    run_step_topic,
)
from omnimarket.nodes.node_event_emit_effect.errors import (
    UnresolvableTransformError,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    resolve_override_transform,
)

pytestmark = pytest.mark.unit

_RUN_STEP_TOPIC = "onex.evt.omnimarket.delegation-run-step.v1"

# The refusal text recorded in the capture log of run
# 070ef15d-7686-4369-9f9c-ca2d507123a1 (eval escalation-forced@8f599e084f77).
_RECORDED_REFUSAL_070EF15D = (
    "Topic override 'onex.evt.omnimarket.delegation-run-step.v1' for event_type "
    "'delegation.run_step' has no declared redaction posture: the topic is "
    "absent from the registry and the event_type is unregistered, so neither "
    "declares what may reach it. Refusing to publish"
)


class _FakePublishAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonType, str | None]] = []

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.calls.append((topic, payload, key))


def _recorded_run_step_request() -> ModelEmitRequest:
    correlation = str(uuid4())
    return ModelEmitRequest(
        event_type="delegation.run_step",
        payload={
            "tenant_id": "tenant-a",
            "correlation_id": correlation,
            "step_sequence": 1,
            "step_kind": "route",
            "started_at": "2026-10-23T02:06:00+00:00",
            "outcome": "succeeded",
            "reason": "reasoning",
        },
        topic=_RUN_STEP_TOPIC,
        event_id=f"delegation-run-step-{correlation}-1",
        correlation_id=correlation,
        partition_key=correlation,
    )


def test_run_step_topic_is_the_one_the_orchestrator_contract_declares() -> None:
    assert run_step_topic() == _RUN_STEP_TOPIC


def test_run_step_publish_posture_resolves_for_the_override() -> None:
    # Declared, needs no transform: the step carries ids, enums and a short
    # reason, no prompt or output text.
    assert resolve_override_transform(_RUN_STEP_TOPIC, "delegation.run_step") is None


def test_run_step_publish_posture_delivers_one_recorded_step(tmp_path: Path) -> None:
    adapter = _FakePublishAdapter()
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    request = _recorded_run_step_request()

    handler.handle(request)

    assert [topic for topic, _, _ in adapter.calls] == [_RUN_STEP_TOPIC]
    assert spool.pending_count() == 0


def test_receipt_070ef15d_run_step_is_no_longer_refused(tmp_path: Path) -> None:
    """Built from the refusal recorded for run 070ef15d: that exact text must
    not be raised by the publish it was recorded against."""
    handler = HandlerEventEmitEffect(
        spool=SpoolOutbox(tmp_path / "spool"),
        publish_adapter=_FakePublishAdapter(),
    )
    try:
        handler.handle(_recorded_run_step_request())
    except UnresolvableTransformError as exc:
        pytest.fail(
            f"refused with the recorded text: {exc}\n"
            f"recorded: {_RECORDED_REFUSAL_070EF15D}"
        )
