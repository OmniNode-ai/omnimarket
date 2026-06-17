# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Semantic validation tests (OMN-13166).

The 2026-06-16 gate-zero stability SEA cell produced a handler that split on
whitespace instead of underscores for a snake_case -> PascalCase task, yet was
projected as contract_passed=true. These tests pin the behavioral layer that
catches that false-green: the whitespace-splitting impostor must FAIL semantic
validation while the correct underscore-splitting handler PASSES.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelNodeGenerationRequest,
)
from omnimarket.nodes.node_generation_consumer.semantic_validation import (
    ModelSemanticFixture,
    ModelSemanticResult,
    derive_semantic_fixtures,
    evaluate_handler_semantics,
)

# Exact handler from the gate-zero stability terminal.json (bda26f6a...).
# Splits on whitespace, not underscores -> "Hello_world" for "hello_world".
_GATE_ZERO_IMPOSTOR = """\
def handle(input_data):
    text = input_data.get("text", "")
    trimmed = text.strip()
    original_case = trimmed.lower()
    pascal_case = "".join(word.capitalize() for word in original_case.split())
    changed = pascal_case != text
    return {
        "pascal_case": pascal_case,
        "changed": changed,
    }
"""

# Control handler from the dev cell (886e77e5...): splits on underscores.
_CORRECT_PASCAL = """\
def handle(input_data):
    text = input_data.get("text", "")
    trimmed = text.strip()
    pascal_case = "".join(word.capitalize() for word in trimmed.split("_"))
    changed = pascal_case != text
    return {
        "pascal_case": pascal_case,
        "changed": changed,
    }
"""

_PASCAL_TASK = (
    "Generate an ONEX compute node named night_probe_pascal_case that accepts "
    "snake_case text, trims it, converts it to PascalCase, and returns "
    "pascal_case plus changed boolean."
)


@pytest.mark.unit
def test_derive_fixtures_for_pascal_task() -> None:
    fixtures = derive_semantic_fixtures(_PASCAL_TASK)
    assert fixtures, "snake->pascal task must derive at least one fixture"
    assert all(isinstance(f, ModelSemanticFixture) for f in fixtures)
    assert all(f.transform == "snake_to_pascal" for f in fixtures)


@pytest.mark.unit
def test_gate_zero_impostor_fails_semantic_validation() -> None:
    """The whitespace-splitting handler must NOT pass behavioral validation."""
    fixtures = derive_semantic_fixtures(_PASCAL_TASK)
    result = evaluate_handler_semantics(_GATE_ZERO_IMPOSTOR, fixtures)
    assert result.checked is True
    assert result.passed is False
    assert result.fixtures_passed < result.fixtures_total
    assert any("snake_to_pascal" in e for e in result.errors)


@pytest.mark.unit
def test_correct_pascal_handler_passes_semantic_validation() -> None:
    fixtures = derive_semantic_fixtures(_PASCAL_TASK)
    result = evaluate_handler_semantics(_CORRECT_PASCAL, fixtures)
    assert result.checked is True
    assert result.passed is True
    assert result.fixtures_passed == result.fixtures_total
    assert result.errors == []


@pytest.mark.unit
def test_unknown_task_is_inconclusive_not_pass() -> None:
    """No derivable invariant -> checked=False, passed=False (never a silent pass)."""
    fixtures = derive_semantic_fixtures(
        "Generate a node that summarizes arbitrary financial reports."
    )
    assert fixtures == []
    result = evaluate_handler_semantics(_CORRECT_PASCAL, fixtures)
    assert result.checked is False
    assert result.passed is False


@pytest.mark.unit
def test_handler_reaching_for_io_is_a_semantic_failure() -> None:
    """A handler that reaches disallowed builtins fails inside the sandbox."""
    io_handler = """\
def handle(input_data):
    with open("/etc/passwd") as fh:
        return {"pascal_case": fh.read()}
"""
    fixtures = derive_semantic_fixtures(_PASCAL_TASK)
    result = evaluate_handler_semantics(io_handler, fixtures)
    assert result.checked is True
    assert result.passed is False
    assert any("raised" in e for e in result.errors)


@pytest.mark.unit
def test_camel_case_invariant() -> None:
    camel_handler = """\
def handle(input_data):
    text = input_data.get("text", "")
    parts = text.strip().split("_")
    camel = parts[0].lower() + "".join(p.capitalize() for p in parts[1:])
    return {"camel_case": camel}
"""
    fixtures = derive_semantic_fixtures(
        "Generate a node that converts snake_case to camelCase."
    )
    result = evaluate_handler_semantics(camel_handler, fixtures)
    assert result.checked is True
    assert result.passed is True


@pytest.mark.unit
def test_result_model_is_frozen() -> None:
    result = ModelSemanticResult(checked=True, passed=True)
    with pytest.raises(ValidationError):
        result.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# End-to-end handler tests: the gate-zero false-green must not pass the gate.
# ---------------------------------------------------------------------------

_PASCAL_CONTRACT_YAML = """\
name: night_probe_pascal_case
contract_version: "1.0.0"
node_type: compute
input_model:
  name: ModelPascalInput
  module: omnimarket.nodes.night_probe_pascal_case.models
output_model:
  name: ModelPascalOutput
  module: omnimarket.nodes.night_probe_pascal_case.models
"""


def _llm_response(handler_source: str) -> str:
    return (
        "```yaml\n" + _PASCAL_CONTRACT_YAML + "```\n\n"
        "```python\n" + handler_source + "```\n"
    )


class _FakeUsage:
    def __init__(self) -> None:
        self.tokens_input = 10
        self.tokens_output = 20
        self.tokens_total = 30
        self.usage_source = "api"


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.generated_text = text
        self.usage = _FakeUsage()
        self.latency_ms = 100.0


class _FakeLlmEffect:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def handle(self, request: Any) -> _FakeResponse:
        await asyncio.sleep(0)
        text = (
            self._responses.pop(0)
            if self._responses
            else _llm_response(_CORRECT_PASCAL)
        )
        return _FakeResponse(text)


def _make_handler(
    responses: list[str], published: list[tuple[str, bytes]]
) -> HandlerGenerationConsumer:
    def _publisher(topic: str, payload: bytes) -> None:
        published.append((topic, payload))

    return HandlerGenerationConsumer(
        effect_handler=_FakeLlmEffect(responses),
        event_publisher=_publisher,
    )


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    # Isolate replay state so handle() never short-circuits on a stale marker.
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_zero_impostor_is_not_a_full_pass_end_to_end() -> None:
    """The gate-zero handler is contract-valid but behaviorally wrong.

    contract_passed must stay true (shape is valid) while semantic_passed is
    false — the false-green is now visible and blocked from deploy.
    """
    published: list[tuple[str, bytes]] = []
    # Only the impostor is offered, repeatedly — every attempt is behaviorally wrong.
    handler = _make_handler(
        [_llm_response(_GATE_ZERO_IMPOSTOR)] * 3, published=published
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description=_PASCAL_TASK,
            correlation_id="corr-gate-zero",
            max_attempts=3,
        )
    )

    assert result.contract_passed is True  # shape was valid
    assert result.semantic_checked is True
    assert result.semantic_passed is False  # behavior was wrong — no false green
    # The behaviorally-wrong node must NOT be deployed or registered.
    deploy_topics = [t for t, _ in published if "node-deploy" in t]
    registration_topics = [t for t, _ in published if "node-registration" in t]
    assert deploy_topics == []
    assert registration_topics == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correct_handler_passes_end_to_end_and_deploys() -> None:
    published: list[tuple[str, bytes]] = []
    handler = _make_handler([_llm_response(_CORRECT_PASCAL)], published=published)

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description=_PASCAL_TASK,
            correlation_id="corr-correct",
            max_attempts=3,
        )
    )

    assert result.contract_passed is True
    assert result.semantic_checked is True
    assert result.semantic_passed is True
    assert result.attempt_count == 1
    deploy_topics = [t for t, _ in published if "node-deploy" in t]
    assert deploy_topics, "behaviorally correct node must deploy"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retries_past_impostor_to_correct_handler() -> None:
    """First attempt behaviorally wrong, second correct — run succeeds on retry."""
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(
        [_llm_response(_GATE_ZERO_IMPOSTOR), _llm_response(_CORRECT_PASCAL)],
        published=published,
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description=_PASCAL_TASK,
            correlation_id="corr-retry-semantic",
            max_attempts=3,
        )
    )

    assert result.attempt_count == 2
    assert result.attempts[0].contract_passed is True
    assert result.attempts[0].semantic_passed is False
    assert result.attempts[1].semantic_passed is True
    assert result.contract_passed is True
    assert result.semantic_passed is True
