# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12996: exp0/generation-consumer captures honest LLM usage provenance.

Before this fix the generation path (experiment runner -> node-generation-requested.v1
-> node_generation_consumer) emitted projection rows with prompt_tokens=0 /
usage_source=MISSING and the benchmark hardcoded EnumUsageSource.ESTIMATED, hollowing
the experiment COST columns on ab-compare.v1 / llm_call_metrics.

These tests drive the REAL dispatch entrypoint (HandlerGenerationConsumer.handle) with
a faked inference effect — handler-isolation tests alone are insufficient per standing
policy — and assert the emitted benchmark + per-attempt records carry honest provenance:

* provider reports a usage block (usage_source="api")  -> MEASURED, nonzero tokens
* provider omits usage (response.usage is None)        -> UNKNOWN, never fabricated MEASURED
* mixed attempts (one MEASURED, one UNKNOWN)            -> MEASURED at run level
* all attempts absent usage                            -> UNKNOWN at run level

ONEX_STATE_DIR is isolated to a tmp dir per test so the handler's replay guard does not
read or write the shared operator state (the replay marker would otherwise short-circuit
handle() and mask the recomputed provenance).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelNodeGenerationRequest,
)

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
    "Here is your node:\n"
    "```yaml\n" + _VALID_CONTRACT_YAML + "```\n\n"
    "```python\n" + _VALID_HANDLER_SOURCE + "```\n"
)


class _FakeUsage:
    """Mirrors omnibase_infra ModelLlmUsage: token counts + usage_source provenance."""

    def __init__(self, inp: int, out: int, usage_source: str) -> None:
        self.tokens_input = inp
        self.tokens_output = out
        self.tokens_total = inp + out
        self.usage_source = usage_source


class _FakeResponseWithUsage:
    def __init__(self, text: str, inp: int, out: int, usage_source: str) -> None:
        self.generated_text = text
        self.usage = _FakeUsage(inp, out, usage_source)
        self.latency_ms = 100.0


class _FakeResponseNoUsage:
    """Provider returned no usage block at all (response.usage is None)."""

    def __init__(self, text: str) -> None:
        self.generated_text = text
        self.usage = None
        self.latency_ms = 100.0


class _SequencedEffect:
    """Returns a fixed sequence of fake responses, one per LLM call."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)

    async def handle(self, request: Any) -> Any:
        await asyncio.sleep(0)
        return self._responses.pop(0)


def _noop_publisher(topic: str, payload: bytes) -> None:
    """Discard emitted events — these tests assert on the returned benchmark."""


def _make_handler(responses: list[Any]) -> HandlerGenerationConsumer:
    return HandlerGenerationConsumer(
        effect_handler=_SequencedEffect(responses),
        event_publisher=_noop_publisher,
    )


@pytest.fixture(autouse=True)
def _isolate_onex_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Isolate the replay-state dir so the handler never reads shared operator state.

    Both ONEX_STATE_DIR and ONEX_STATE_ROOT are pointed at a per-test tmp dir; the
    handler's _resolve_state_root reads either key.
    """
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "onex_state"))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_measured_usage_captured_when_provider_reports_tokens() -> None:
    """A provider-reported usage block yields MEASURED provenance + nonzero tokens.

    This is the core demo-relevant case: an exp0 row must show usage_source=MEASURED
    with real prompt/completion tokens so the cost columns and ROI math are populated.
    """
    handler = _make_handler(
        [
            _FakeResponseWithUsage(
                _VALID_LLM_RESPONSE, inp=128, out=64, usage_source="api"
            )
        ]
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="omn12996-measured-1",
        )
    )

    assert result.usage_source == EnumUsageSource.MEASURED
    assert result.prompt_tokens == 128
    assert result.completion_tokens == 64
    assert result.attempts[0].usage_source == EnumUsageSource.MEASURED
    assert result.attempts[0].token_usage_input == 128
    assert result.attempts[0].token_usage_output == 64


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_usage_stays_unknown_never_fabricated_measured() -> None:
    """No provider usage block -> UNKNOWN provenance, never a fabricated MEASURED.

    Honest absent-state: tokens are zero AND the row says UNKNOWN (not ESTIMATED with
    fake zeros, and never MEASURED).
    """
    handler = _make_handler([_FakeResponseNoUsage(_VALID_LLM_RESPONSE)])

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="omn12996-missing-1",
        )
    )

    assert result.usage_source == EnumUsageSource.UNKNOWN
    assert result.usage_source != EnumUsageSource.MEASURED
    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
    assert result.attempts[0].usage_source == EnumUsageSource.UNKNOWN


@pytest.mark.unit
@pytest.mark.asyncio
async def test_estimated_usage_source_propagates() -> None:
    """A locally-estimated usage block ("estimated") propagates as ESTIMATED, not MEASURED."""
    handler = _make_handler(
        [
            _FakeResponseWithUsage(
                _VALID_LLM_RESPONSE, inp=50, out=25, usage_source="estimated"
            )
        ]
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="omn12996-estimated-1",
        )
    )

    assert result.usage_source == EnumUsageSource.ESTIMATED
    assert result.prompt_tokens == 50
    assert result.completion_tokens == 25


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_level_provenance_is_measured_if_any_attempt_measured() -> None:
    """Retry path: a MEASURED attempt makes the run-level provenance MEASURED.

    First attempt fails validation with no usage (UNKNOWN); second attempt succeeds with
    a provider usage block (MEASURED). The aggregated benchmark must report MEASURED and
    sum the real tokens across attempts.
    """
    _invalid_response = (
        "```yaml\nnot_a_mapping: [broken\n```\n\n"
        "```python\n" + _VALID_HANDLER_SOURCE + "```\n"
    )
    handler = _make_handler(
        [
            _FakeResponseNoUsage(_invalid_response),
            _FakeResponseWithUsage(
                _VALID_LLM_RESPONSE, inp=100, out=40, usage_source="api"
            ),
        ]
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="omn12996-mixed-1",
            max_attempts=2,
        )
    )

    assert result.attempt_count == 2
    assert result.attempts[0].usage_source == EnumUsageSource.UNKNOWN
    assert result.attempts[1].usage_source == EnumUsageSource.MEASURED
    assert result.usage_source == EnumUsageSource.MEASURED
    # Tokens sum across attempts: attempt 1 had none, attempt 2 had 100/40.
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 40


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_level_provenance_unknown_when_all_attempts_absent() -> None:
    """All attempts absent usage -> run-level UNKNOWN (the hollow-row case made honest)."""
    _invalid_response = (
        "```yaml\nnot_a_mapping: [broken\n```\n\n"
        "```python\n" + _VALID_HANDLER_SOURCE + "```\n"
    )
    handler = _make_handler(
        [
            _FakeResponseNoUsage(_invalid_response),
            _FakeResponseNoUsage(_invalid_response),
        ]
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="omn12996-allmissing-1",
            max_attempts=2,
        )
    )

    assert result.usage_source == EnumUsageSource.UNKNOWN
    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
