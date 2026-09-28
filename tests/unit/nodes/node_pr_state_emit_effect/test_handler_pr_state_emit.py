# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The handler delegates delivery and identity to the existing spool."""

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
    ModelEmitResult,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from omnimarket.nodes.node_pr_state_emit_effect.handlers.handler_pr_state_emit import (
    HandlerPrStateEmit,
)
from omnimarket.nodes.node_pr_state_emit_effect.models.model_pr_state_emit_request import (
    ModelPrStateEmitRequest,
)
from tests.unit.nodes.node_pr_state_emit_effect.helpers import event

pytestmark = pytest.mark.unit


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.requests: list[ModelEmitRequest] = []
        self.fail = fail

    def handle(self, request: ModelEmitRequest) -> ModelEmitResult:
        if self.fail:
            raise RuntimeError("spool refused")
        self.requests.append(request)
        return ModelEmitResult(event_id=request.event_id, published=True)


def test_emit_types_request_and_preserves_identity_and_partition() -> None:
    e = event()
    recorder = Recorder()
    request = ModelPrStateEmitRequest.model_validate(e.model_dump(exclude={"digest"}))
    result = HandlerPrStateEmit(emitter=recorder).handle(request)
    assert result.accepted
    assert result.published
    assert result.digest == e.digest
    sent = recorder.requests[0]
    assert sent.event_type == "pr.state.observed"
    assert sent.event_id == sent.correlation_id == e.digest
    assert sent.partition_key == "OmniNode-ai/omnimarket#3050"
    assert sent.payload == e.model_dump(mode="json")


def test_spool_refusal_is_typed() -> None:
    result = HandlerPrStateEmit(emitter=Recorder(True)).handle(event())
    assert not result.accepted
    assert "spool refused" in (result.error or "")


def test_real_spool_preserves_retry_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_ONLY", "true")
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerPrStateEmit(emitter=HandlerEventEmitEffect(spool=spool))
    for _ in range(2):
        result = handler.handle(event())
        assert result.accepted
        assert not result.published
    records = list((tmp_path / "spool").glob("*.json"))
    # Each append has its own sequence-prefixed file; delivery identity survives.
    assert len(records) == 2
    for path in records:
        record = json.loads(path.read_text())
        assert record["partition_key"] == "OmniNode-ai/omnimarket#3050"
        assert record["payload"]["digest"] == event().digest
        assert record["event_id"] == record["correlation_id"] == event().digest


def test_runtime_adapter_accepts_json_request() -> None:
    from omnibase_core.runtime.runtime_local_adapter import _invoke_handle_method

    recorder = Recorder()
    result = _invoke_handle_method(
        HandlerPrStateEmit(emitter=recorder).handle,
        event().model_dump(mode="json", exclude={"digest"}),
    )
    assert result.accepted
    assert recorder.requests[0].event_id == event().digest
