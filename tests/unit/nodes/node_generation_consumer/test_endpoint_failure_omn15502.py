# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Truthful empty-artifact and endpoint-failure terminals (OMN-15502)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from omnibase_core.enums.enum_routing_error_class import RoutingErrorClass
from omnibase_infra.protocols import ProtocolEventBusLike
from omnibase_infra.runtime.event_bus_subcontract_wiring import (
    load_published_events_map,
)
from omnibase_infra.runtime.service_dispatch_result_applier import (
    DispatchResultApplier,
)

from omnimarket.nodes.node_generation_consumer.handlers import (
    handler_generation_consumer as generation_module,
)
from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
    ModelActiveRoute,
    ModelResolvedEndpoint,
    _GenerationCallOutcome,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelGenerationCompleted,
    ModelGenerationFailed,
    ModelNodeGenerationRequest,
)

_RESOLVED_ENDPOINT = "https://generation.example/v1/chat/completions"
_VALID_RESPONSE = """\
```yaml
name: node_stub_compute
contract_version: "1.0.0"
node_type: compute
input_model:
  name: ModelStubInput
  module: omnimarket.nodes.node_stub_compute.models
output_model:
  name: ModelStubOutput
  module: omnimarket.nodes.node_stub_compute.models
```

```python
def handle(input_data):
    return {"result": input_data}
```
"""


class _Usage:
    tokens_input = 10
    tokens_output = 20
    usage_source = "api"


class _Response:
    def __init__(
        self, *, text: str = _VALID_RESPONSE, usage: _Usage | None = None
    ) -> None:
        self.generated_text = text
        self.usage = usage


class _RecordingEffect:
    def __init__(
        self, *, text: str = _VALID_RESPONSE, usage: _Usage | None = None
    ) -> None:
        self.text = text
        self.usage = usage
        self.call_count = 0

    async def handle(self, _request: Any) -> _Response:
        await asyncio.sleep(0)
        self.call_count += 1
        return _Response(text=self.text, usage=self.usage)


@pytest.fixture(autouse=True)
def _isolate_replay_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "onex-state"))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)


def _resolved_endpoint() -> ModelResolvedEndpoint:
    return ModelResolvedEndpoint(
        endpoint_url=_RESOLVED_ENDPOINT,
        provider="local",
        served_model_id="Qwen3.6-35B-A3B",
        max_tokens=4096,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reused_handler_preserves_endpoint_failure_as_failed_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later resolution failure cannot inherit an earlier run's endpoint."""
    resolution_count = 0

    def _resolve(**_kwargs: str) -> ModelResolvedEndpoint:
        nonlocal resolution_count
        resolution_count += 1
        if resolution_count == 1:
            return _resolved_endpoint()
        raise ValueError("backend is absent from the routing authority")

    monkeypatch.setattr(generation_module, "resolve_generation_endpoint", _resolve)
    effect = _RecordingEffect(usage=_Usage())
    handler = HandlerGenerationConsumer(event_publisher=lambda _topic, _payload: None)
    handler._effect = effect
    handler._injected_effect = False

    first = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build the first stub node",
            correlation_id="omn-15502-warm-success",
            max_attempts=1,
        )
    )
    failed = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build the second stub node",
            correlation_id="omn-15502-endpoint-unavailable",
            max_attempts=1,
        )
    )

    assert type(first) is ModelGenerationCompleted
    assert first.resolved_endpoint == _RESOLVED_ENDPOINT
    assert type(failed) is ModelGenerationFailed
    assert failed.failure_class is RoutingErrorClass.ENDPOINT_UNAVAILABLE
    assert "endpoint_ref='local-coder'" in failed.failure_reason
    assert "backend is absent from the routing authority" in failed.failure_reason
    assert failed.resolved_endpoint == ""
    assert failed.contract_yaml == ""
    assert failed.handler_source == ""
    assert failed.prompt_tokens == 0
    assert failed.completion_tokens == 0
    assert failed.attempts[-1].failure_class is RoutingErrorClass.ENDPOINT_UNAVAILABLE
    assert failed.attempts[-1].failure_reason == failed.failure_reason
    assert failed.attempts[-1].validation_errors == [failed.failure_reason]
    assert effect.call_count == 1

    contract_path = (
        Path(generation_module.__file__).resolve().parent.parent / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    applier = DispatchResultApplier(
        event_bus=MagicMock(spec=ProtocolEventBusLike),
        output_topic=contract["terminal_event"],
        output_topic_map=load_published_events_map(contract_path),
        allowed_output_topics=contract["event_bus"]["publish_topics"],
    )
    assert (
        applier._resolve_output_topic(failed)
        == (contract["runtime_dispatch"]["terminal_events"]["failure"])
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_artifact_with_unknown_zero_usage_remains_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent usage is not evidence that a non-empty valid artifact failed."""
    monkeypatch.setattr(
        generation_module,
        "resolve_generation_endpoint",
        lambda **_kwargs: _resolved_endpoint(),
    )
    effect = _RecordingEffect(usage=None)
    handler = HandlerGenerationConsumer(event_publisher=lambda _topic, _payload: None)
    handler._effect = effect
    handler._injected_effect = False

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a valid zero-usage stub node",
            correlation_id="omn-15502-valid-zero-usage",
            max_attempts=1,
        )
    )

    assert type(result) is ModelGenerationCompleted
    assert result.contract_yaml
    assert result.handler_source
    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
    assert result.failure_class is None
    assert result.failure_reason == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_provider_artifact_is_an_explicit_failed_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A returned empty body is failure evidence, not a completed artifact."""
    monkeypatch.setattr(
        generation_module,
        "resolve_generation_endpoint",
        lambda **_kwargs: _resolved_endpoint(),
    )
    effect = _RecordingEffect(text="", usage=_Usage())
    handler = HandlerGenerationConsumer(event_publisher=lambda _topic, _payload: None)
    handler._effect = effect
    handler._injected_effect = False

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a node from an empty provider response",
            correlation_id="omn-15502-empty-provider-artifact",
            max_attempts=1,
        )
    )

    assert type(result) is ModelGenerationFailed
    assert result.failure_class is None
    assert result.failure_reason == (
        "LLM inference returned empty generated_text for endpoint_ref='local-coder'"
    )
    assert result.resolved_endpoint == _RESOLVED_ENDPOINT
    assert result.contract_yaml == ""
    assert result.handler_source == ""
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 20
    assert result.attempts[-1].validation_errors == [result.failure_reason]
    assert effect.call_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_generation_retries_keep_their_own_active_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One coroutine cannot replace another run's retry route or endpoint."""
    route_a = ModelActiveRoute(
        tier_name="local",
        provider="local",
        served_model_id="model-a-local",
        endpoint_ref="a-local",
        authority_resolved=True,
        endpoint_url="https://a-local.example/v1/chat/completions",
        max_tokens=4096,
    )
    route_a_escalated = SimpleNamespace(
        tier_name="cheap_cloud",
        selected_model="model-a-escalated",
        endpoint_url="https://a-escalated.example/v1/chat/completions",
        api_key_ref=None,
        max_tokens=4096,
    )
    route_b = ModelActiveRoute(
        tier_name="cheap_cloud",
        provider="cloud",
        served_model_id="model-b",
        endpoint_ref="b-cloud",
        authority_resolved=True,
        endpoint_url="https://b.example/v1/chat/completions",
        max_tokens=4096,
    )
    a_entered = asyncio.Event()
    b_entered = asyncio.Event()
    seen: list[tuple[str, int, str, str]] = []

    async def _call_llm(
        _self: HandlerGenerationConsumer,
        task_description: str,
        attempt: int,
        *,
        route: ModelActiveRoute,
        previous_errors: list[str] | None = None,
        context_pack: str = "",
    ) -> _GenerationCallOutcome:
        del previous_errors, context_pack
        run = "A" if task_description == "run-a" else "B"
        seen.append((run, attempt, route.served_model_id, route.endpoint_url))
        if run == "A" and attempt == 1:
            a_entered.set()
            await b_entered.wait()
            return _GenerationCallOutcome(
                raw_output="not a generated artifact",
                resolved_endpoint=route.endpoint_url,
            )
        if run == "B":
            await a_entered.wait()
            b_entered.set()
        return _GenerationCallOutcome(
            raw_output=_VALID_RESPONSE,
            input_tokens=10,
            output_tokens=20,
            resolved_endpoint=route.endpoint_url,
        )

    handler = HandlerGenerationConsumer(
        effect_handler=object(),
        event_publisher=lambda _topic, _payload: None,
    )
    monkeypatch.setattr(handler, "_starting_route", lambda: route_a)
    monkeypatch.setattr(
        handler,
        "_forced_starting_route",
        lambda **_kwargs: route_b,
    )
    monkeypatch.setattr(
        handler,
        "_resolve_escalation_decision",
        lambda **_kwargs: route_a_escalated,
    )
    monkeypatch.setattr(HandlerGenerationConsumer, "_call_llm", _call_llm)

    result_a, result_b = await asyncio.gather(
        handler.handle(
            ModelNodeGenerationRequest(
                task_description="run-a",
                correlation_id="omn-15502-concurrent-a",
                max_attempts=2,
            )
        ),
        handler.handle(
            ModelNodeGenerationRequest(
                task_description="run-b",
                correlation_id="omn-15502-concurrent-b",
                max_attempts=1,
                forced_endpoint_ref="b-cloud",
            )
        ),
    )

    assert ("A", 2, "model-a-escalated", route_a_escalated.endpoint_url) in seen
    assert result_a.model_id == "model-a-escalated"
    assert result_a.resolved_endpoint == route_a_escalated.endpoint_url
    assert result_b.model_id == "model-b"
    assert result_b.resolved_endpoint == route_b.endpoint_url
