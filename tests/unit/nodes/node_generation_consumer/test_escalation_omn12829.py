# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12829 (C1): generation contract-validation-failure escalation.

On contract-validation failure WITH attempts remaining, the generation consumer
calls the ROUTING AUTHORITY (node_delegation_routing_reducer.delta) with the
escalation context and emits onex.evt.omnimarket.delegation-escalation-triggered.v1.

Architecture boundary (non-negotiable): the escalation authority is owned by
routing — the generation consumer does NOT select the next model itself. It asks
the authority for the next tier/model/endpoint and records what the authority
decided.

Acceptance: the escalation proof records tier, provider, model, endpoint,
attempt_count, escalation_reason.

All tests use FakeLlmEffect — no network, no Kafka, no Docker.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.delegation.llm_cost_routing.model_generation_escalation_event import (
    ModelGenerationEscalationTriggeredEvent,
)
from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelNodeGenerationRequest,
)

_ESCALATION_TOPIC = "onex.evt.omnimarket.delegation-escalation-triggered.v1"

_VALID_CONTRACT_YAML = """\
name: node_stub_compute
contract_version: "1.0.0"
node_type: compute
input_model:
  name: ModelStubInput
  module: omnimarket.nodes.node_stub_compute.models
output_model:
  name: ModelStubOutput
  module: omnimarket.nodes.node_stub_compute.models
"""

_VALID_HANDLER_SOURCE = """\
def handle(input_data):
    return {"result": input_data}
"""

_VALID_LLM_RESPONSE = (
    "```yaml\n" + _VALID_CONTRACT_YAML + "```\n\n"
    "```python\n" + _VALID_HANDLER_SOURCE + "```\n"
)

_INVALID_LLM_RESPONSE = (
    "```yaml\nnot_a_mapping: [broken\n```\n\n"
    "```python\n" + _VALID_HANDLER_SOURCE + "```\n"
)


class _FakeUsage:
    def __init__(self, inp: int = 10, out: int = 20) -> None:
        self.tokens_input = inp
        self.tokens_output = out
        self.tokens_total = inp + out


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.generated_text = text
        self.usage = _FakeUsage()
        self.latency_ms = 100.0


class FakeLlmEffect:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def handle(self, request: Any) -> _FakeResponse:
        await asyncio.sleep(0)
        text = self._responses.pop(0) if self._responses else _VALID_LLM_RESPONSE
        return _FakeResponse(text)


def _contract_with_escalation_topic(tmp_path: Path) -> Path:
    """Generation contract declaring the escalation topic + code_generation routing."""
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {
            "publish_topics": [
                "onex.evt.omnimarket.node-generation-completed.v1",
                "onex.evt.omnimarket.node-generation-failed.v1",
                "onex.evt.platform.node-registration.v1",
                "onex.cmd.omnimarket.node-deploy.v1",
                _ESCALATION_TOPIC,
            ],
            "subscribe_topics": [],
        },
        "model_routing": {
            "provider": "local",
            "served_model_id": "Qwen3.6-35B-A3B",
            "endpoint_ref": "local-coder",
            "routing_source": "contract",
            # The task class drives the routing-authority escalation ladder.
            "task_type": "code_generation",
        },
    }
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.dump(contract))
    return path


def _make_handler(
    tmp_path: Path,
    responses: list[str],
    published: list[tuple[str, bytes]],
) -> HandlerGenerationConsumer:
    return HandlerGenerationConsumer(
        effect_handler=FakeLlmEffect(responses),
        event_publisher=lambda t, p: published.append((t, p)),
        contract_path=_contract_with_escalation_topic(tmp_path),
    )


def _escalation_events(
    published: list[tuple[str, bytes]],
) -> list[dict[str, Any]]:
    return [json.loads(p) for t, p in published if t == _ESCALATION_TOPIC]


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_escalation_event_records_required_acceptance_fields() -> None:
    """The escalation proof model must carry tier/provider/model/endpoint/attempt/reason."""
    event = ModelGenerationEscalationTriggeredEvent(
        correlation_id="corr-1",
        task_type="code_generation",
        tier="cheap_cloud",
        provider="cloud",
        model="openrouter-glm-flash",
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        attempt_count=2,
        escalation_reason="schema: contract YAML did not parse to a mapping",
    )
    assert event.tier == "cheap_cloud"
    assert event.provider == "cloud"
    assert event.model == "openrouter-glm-flash"
    assert event.endpoint.endswith("/chat/completions")
    assert event.attempt_count == 2
    assert event.escalation_reason


# ---------------------------------------------------------------------------
# Behaviour: escalation emitted on contract-validation failure with attempts left
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_escalation_emitted_on_validation_failure_with_attempts_remaining(
    tmp_path: Path,
) -> None:
    """A failed attempt with attempts remaining emits the escalation event."""
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(
        tmp_path,
        [_INVALID_LLM_RESPONSE, _VALID_LLM_RESPONSE],
        published,
    )

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-escalate-1",
            max_attempts=2,
        )
    )

    events = _escalation_events(published)
    assert len(events) == 1, f"expected exactly one escalation event, got {events}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_escalation_on_first_pass_success(tmp_path: Path) -> None:
    """A first-attempt success must not trigger escalation."""
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(tmp_path, [_VALID_LLM_RESPONSE], published)

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-no-escalate-1",
        )
    )

    assert _escalation_events(published) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_escalation_on_final_attempt_failure(tmp_path: Path) -> None:
    """The final (budget-exhausting) failed attempt has no attempts remaining → no escalation."""
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(tmp_path, [_INVALID_LLM_RESPONSE], published)

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-final-fail-1",
            max_attempts=1,
        )
    )

    assert _escalation_events(published) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_escalation_event_records_authority_decision(tmp_path: Path) -> None:
    """The emitted escalation event records what the ROUTING AUTHORITY decided.

    The generation consumer does not select the model itself — the tier/model/
    endpoint recorded must match the routing authority's decision for the
    escalated tier. The proof records tier, provider, model, endpoint,
    attempt_count, escalation_reason.
    """
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(
        tmp_path,
        [_INVALID_LLM_RESPONSE, _VALID_LLM_RESPONSE],
        published,
    )

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-authority-1",
            max_attempts=2,
        )
    )

    events = _escalation_events(published)
    assert len(events) == 1
    event = events[0]
    # Acceptance fields all present + non-empty.
    assert event["correlation_id"] == "corr-authority-1"
    assert event["task_type"] == "code_generation"
    assert event["tier"], "tier must be recorded from the routing authority"
    assert event["provider"], "provider must be recorded"
    assert event["model"], "model must be recorded from the routing authority"
    assert event["endpoint"].startswith("http"), (
        "endpoint must be the complete URL the routing authority resolved"
    )
    assert event["attempt_count"] == 1, "attempt_count is the failed attempt number"
    assert "mapping" in event["escalation_reason"] or event["escalation_reason"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_escalation_tier_is_not_the_starting_tier(tmp_path: Path) -> None:
    """The authority escalates to a DIFFERENT tier than the consumer's starting tier.

    The generation consumer starts on tier 'local' (endpoint_ref=local-coder).
    code_generation escalation_policy.tier_order is [cheap_cloud, local, claude];
    the authority must select the next tier in that ladder, not re-pick 'local'.
    """
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(
        tmp_path,
        [_INVALID_LLM_RESPONSE, _VALID_LLM_RESPONSE],
        published,
    )

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-tier-ladder-1",
            max_attempts=2,
        )
    )

    events = _escalation_events(published)
    assert len(events) == 1
    # The starting tier is 'local'; the escalated tier must be the next eligible
    # tier in the authority's ladder.
    assert events[0]["tier"] != "local"
