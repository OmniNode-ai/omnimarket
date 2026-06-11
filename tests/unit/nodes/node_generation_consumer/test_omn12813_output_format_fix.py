# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12813: SEA generation output-format fix tests.

Coverage:
- The local-qwen-generation-no-think inference protocol profile fires for the
  generation system prompt, prepending /no_think to the user prompt.
- The local-qwen-generation-exemplar inference protocol profile fires for the
  generation system prompt, appending the exemplar to the system prompt.
- The combined effect means the outbound system prompt contains the exemplar
  and the outbound user prompt is prefixed with /no_think.
- A well-formed two-fenced-block response yields contract_passed=True with
  populated contract_yaml and handler_source via validate_generation().
- Non-Qwen models are not affected by the generation profiles.
- The _DEFAULT_SYSTEM_PROMPT contains the canonical "ONEX node generator" marker
  required for the inference protocol profile to fire.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.inference.protocol_config import (
    apply_inference_protocol,
    load_inference_protocol_config,
)
from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    _DEFAULT_SYSTEM_PROMPT,
    HandlerGenerationConsumer,
    _validate_generation,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelNodeGenerationRequest,
)

# ---------------------------------------------------------------------------
# Shared fixtures — same valid response shape used across generation tests
# ---------------------------------------------------------------------------

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

# Simulates what Qwen3.6-35B-A3B produced before this fix: reasoning prose
# followed by invalid YAML (no fenced blocks at all).
_QWEN_REASONING_PROSE_RESPONSE = (
    "1. Analyze the Request\n"
    "Let me break down the task step by step.\n"
    "First, I need to understand the purpose of this node...\n"
    "2. Design the Contract\n"
    "The contract should specify...\n"
    "This is a compute node so node_type: compute\n"
    "name: node_example\n"  # bare YAML — NOT in a fenced block
)


class _FakeUsage:
    def __init__(self, inp: int = 10, out: int = 20, usage_source: str = "api") -> None:
        self.tokens_input = inp
        self.tokens_output = out
        self.tokens_total = inp + out
        # OMN-12996: provider-reported provenance ("api" -> MEASURED).
        self.usage_source = usage_source


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
        text = self._responses.pop(0) if self._responses else _VALID_LLM_RESPONSE
        return _FakeResponse(text)


def _make_handler(
    responses: list[str],
    published: list[tuple[str, bytes]] | None = None,
) -> HandlerGenerationConsumer:
    captures: list[tuple[str, bytes]] = [] if published is None else published

    def _publisher(topic: str, payload: bytes) -> None:
        captures.append((topic, payload))

    return HandlerGenerationConsumer(
        effect_handler=_FakeLlmEffect(responses),
        event_publisher=_publisher,
    )


@pytest.fixture(autouse=True)
def _isolate_onex_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Isolate the replay-state dir per test (OMN-12996).

    Point both state-root env keys at a per-test tmp dir so handle() never reads
    or writes the shared operator ONEX_STATE_DIR and stays order-independent.
    """
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "onex_state"))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)


# ---------------------------------------------------------------------------
# _DEFAULT_SYSTEM_PROMPT contains the canonical marker
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_system_prompt_contains_onex_node_generator_marker() -> None:
    """_DEFAULT_SYSTEM_PROMPT must contain 'ONEX node generator' for profile match."""
    assert "ONEX node generator" in _DEFAULT_SYSTEM_PROMPT, (
        "_DEFAULT_SYSTEM_PROMPT must contain the exact string 'ONEX node generator' "
        "so the local-qwen-generation-* inference protocol profiles fire. "
        f"Got:\n{_DEFAULT_SYSTEM_PROMPT!r}"
    )


@pytest.mark.unit
def test_default_system_prompt_forbids_analysis_prose() -> None:
    """_DEFAULT_SYSTEM_PROMPT must instruct the model not to emit analysis text."""
    lowered = _DEFAULT_SYSTEM_PROMPT.lower()
    # At least one of these directives must be present.
    has_no_prose_directive = (
        "nothing else" in lowered
        or "do not" in lowered
        or "no chain-of-thought" in lowered
        or "only output" in lowered
        or ("only" in lowered and "two fenced" in lowered)
    )
    assert has_no_prose_directive, (
        "_DEFAULT_SYSTEM_PROMPT must tell the model to emit ONLY the two fenced blocks "
        f"and suppress analysis/explanation. Got:\n{_DEFAULT_SYSTEM_PROMPT!r}"
    )


# ---------------------------------------------------------------------------
# Inference protocol profile: local-qwen-generation-no-think
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_qwen_generation_profile_adds_no_think_prefix() -> None:
    """local-qwen-generation-no-think profile prepends /no_think for Qwen + node_generation."""
    _, out_prompt, _ = apply_inference_protocol(
        system_prompt=_DEFAULT_SYSTEM_PROMPT,
        prompt="Task: Build a stub node",
        model="Qwen3.6-35B-A3B",
        task_type="node_generation",
    )
    assert out_prompt.lstrip().startswith("/no_think"), (
        "local-qwen-generation-no-think profile must prepend /no_think to the user prompt "
        f"for Qwen models with task_type=node_generation. Got: {out_prompt[:80]!r}"
    )


@pytest.mark.unit
def test_non_qwen_model_not_affected_by_generation_profile() -> None:
    """Generation profiles must NOT fire for non-Qwen models."""
    _, out_prompt, _ = apply_inference_protocol(
        system_prompt=_DEFAULT_SYSTEM_PROMPT,
        prompt="Task: Build a stub node",
        model="gemini-2.0-flash",
        task_type="node_generation",
    )
    assert not out_prompt.lstrip().startswith("/no_think"), (
        "local-qwen-generation-no-think must not apply to non-Qwen models. "
        f"Got: {out_prompt[:80]!r}"
    )


# ---------------------------------------------------------------------------
# Inference protocol profile: local-qwen-generation-exemplar
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_qwen_generation_exemplar_appended_to_system_prompt() -> None:
    """local-qwen-generation-exemplar profile appends the exemplar to the system prompt."""
    out_system, _, _ = apply_inference_protocol(
        system_prompt=_DEFAULT_SYSTEM_PROMPT,
        prompt="Task: Build a stub node",
        model="Qwen3.6-35B-A3B",
        task_type="node_generation",
    )
    # The exemplar declares the required output format.
    assert "```yaml" in out_system, (
        "local-qwen-generation-exemplar must append a ```yaml block to the system prompt. "
        f"Got system prompt:\n{out_system[:300]!r}"
    )
    assert "```python" in out_system, (
        "local-qwen-generation-exemplar must append a ```python block to the system prompt. "
        f"Got system prompt:\n{out_system[:300]!r}"
    )
    assert "def handle" in out_system, (
        "The exemplar appended to the system prompt must include a handle() function. "
        f"Got system prompt:\n{out_system[:300]!r}"
    )


@pytest.mark.unit
def test_exemplar_format_directive_contains_nothing_else_instruction() -> None:
    """The exemplar suffix must tell the model to output ONLY the two fenced blocks."""
    out_system, _, _ = apply_inference_protocol(
        system_prompt=_DEFAULT_SYSTEM_PROMPT,
        prompt="Task: Build a stub node",
        model="Qwen3.6-35B-A3B",
        task_type="node_generation",
    )
    lowered = out_system.lower()
    has_format_directive = (
        "nothing else" in lowered
        or ("only" in lowered and "two fenced" in lowered)
        or "do not include" in lowered
    )
    assert has_format_directive, (
        "The system prompt (after exemplar injection) must contain an explicit "
        "format directive telling the model to emit only the two fenced blocks. "
        f"Got system prompt:\n{out_system[:400]!r}"
    )


@pytest.mark.unit
def test_exemplar_not_appended_for_non_qwen_model() -> None:
    """The exemplar profile must NOT fire for non-Qwen models."""
    out_system, _, _ = apply_inference_protocol(
        system_prompt=_DEFAULT_SYSTEM_PROMPT,
        prompt="Task: Build a stub node",
        model="claude-opus-4-8",
        task_type="node_generation",
    )
    # System prompt must remain the base prompt (no exemplar appended).
    assert out_system == _DEFAULT_SYSTEM_PROMPT, (
        "local-qwen-generation-exemplar must not modify the system prompt for non-Qwen models. "
        f"System prompt changed to:\n{out_system!r}"
    )


# ---------------------------------------------------------------------------
# apply_once: profiles must not double-apply
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_think_not_doubled_on_second_apply() -> None:
    """/no_think prefix must not be inserted twice when apply_once is true."""
    _, first_pass, _ = apply_inference_protocol(
        system_prompt=_DEFAULT_SYSTEM_PROMPT,
        prompt="Task: Build a stub node",
        model="Qwen3.6-35B-A3B",
        task_type="node_generation",
    )
    _, second_pass, _ = apply_inference_protocol(
        system_prompt=_DEFAULT_SYSTEM_PROMPT,
        prompt=first_pass,
        model="Qwen3.6-35B-A3B",
        task_type="node_generation",
    )
    assert second_pass.count("/no_think") == 1, (
        "/no_think must appear exactly once even after two apply_inference_protocol passes. "
        f"Got: {second_pass[:120]!r}"
    )


# ---------------------------------------------------------------------------
# Production config declares local-qwen-generation-* profiles
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_production_config_declares_generation_no_think_profile() -> None:
    """inference_protocols.v1.yaml must declare local-qwen-generation-no-think."""
    config = load_inference_protocol_config()
    ids = {p.profile_id for p in config.profiles}
    assert "local-qwen-generation-no-think" in ids, (
        "inference_protocols.v1.yaml must declare profile 'local-qwen-generation-no-think'. "
        f"Found profiles: {sorted(ids)}"
    )


@pytest.mark.unit
def test_production_config_declares_generation_exemplar_profile() -> None:
    """inference_protocols.v1.yaml must declare local-qwen-generation-exemplar."""
    config = load_inference_protocol_config()
    ids = {p.profile_id for p in config.profiles}
    assert "local-qwen-generation-exemplar" in ids, (
        "inference_protocols.v1.yaml must declare profile 'local-qwen-generation-exemplar'. "
        f"Found profiles: {sorted(ids)}"
    )


@pytest.mark.unit
def test_generation_no_think_profile_scoped_to_node_generation_task_type() -> None:
    """local-qwen-generation-no-think must be scoped to task_type='node_generation'."""
    config = load_inference_protocol_config()
    profile = next(
        (
            p
            for p in config.profiles
            if p.profile_id == "local-qwen-generation-no-think"
        ),
        None,
    )
    assert profile is not None
    assert "node_generation" in profile.task_types, (
        "local-qwen-generation-no-think.task_types must include 'node_generation' "
        "to avoid matching unrelated Qwen calls. "
        f"Got task_types: {profile.task_types}"
    )


@pytest.mark.unit
def test_generation_exemplar_profile_scoped_to_node_generation_task_type() -> None:
    """local-qwen-generation-exemplar must be scoped to task_type='node_generation'."""
    config = load_inference_protocol_config()
    profile = next(
        (
            p
            for p in config.profiles
            if p.profile_id == "local-qwen-generation-exemplar"
        ),
        None,
    )
    assert profile is not None
    assert "node_generation" in profile.task_types, (
        "local-qwen-generation-exemplar.task_types must include 'node_generation' "
        "to avoid matching unrelated Qwen calls. "
        f"Got task_types: {profile.task_types}"
    )


@pytest.mark.unit
def test_generation_profiles_do_not_fire_for_other_task_types() -> None:
    """Generation profiles must NOT modify prompts for non-generation task types."""
    out_system, _, _ = apply_inference_protocol(
        system_prompt="You are a summarization assistant.",
        prompt="Summarize the evidence.",
        model="Qwen3.6-35B-A3B",
        task_type="summarization",
    )
    assert "```yaml" not in out_system, (
        "local-qwen-generation-exemplar must not fire for task_type='summarization'. "
        f"Got system prompt:\n{out_system[:200]!r}"
    )


# ---------------------------------------------------------------------------
# validate_generation: well-formed response → contract_passed=True
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_generation_passes_on_well_formed_fenced_response() -> None:
    """A well-formed two-fenced-block response yields valid=True with non-empty fields."""
    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        _extract_blocks,
    )

    contract_yaml, handler_source = _extract_blocks(_VALID_LLM_RESPONSE)
    result = _validate_generation(contract_yaml, handler_source)

    assert result["valid"] is True, (
        f"validate_generation must pass for a well-formed response. "
        f"Errors: {result['errors']}"
    )
    assert contract_yaml.strip(), "contract_yaml must be non-empty for a valid response"
    assert handler_source.strip(), (
        "handler_source must be non-empty for a valid response"
    )


@pytest.mark.unit
def test_validate_generation_fails_on_reasoning_prose_response() -> None:
    """Qwen reasoning-prose response (no fenced blocks) must yield valid=False."""
    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        _extract_blocks,
    )

    contract_yaml, handler_source = _extract_blocks(_QWEN_REASONING_PROSE_RESPONSE)
    result = _validate_generation(contract_yaml, handler_source)

    assert result["valid"] is False, (
        "validate_generation must fail for a reasoning-prose response with no fenced blocks. "
        f"Got result: {result}"
    )
    # handler_source must be empty (no fenced python block found).
    assert not handler_source.strip(), (
        "handler_source must be empty when the response has no ```python block. "
        f"Got: {handler_source!r}"
    )


# ---------------------------------------------------------------------------
# HandlerGenerationConsumer._call_llm wires apply_inference_protocol
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_applies_inference_protocol_to_system_prompt() -> None:
    """HandlerGenerationConsumer must call apply_inference_protocol so Qwen profiles fire.

    Verifies that for the production contract (Qwen3.6-35B-A3B), the system
    prompt delivered to the LLM contains the exemplar injected by the
    local-qwen-generation-exemplar profile.
    """
    system_prompts_seen: list[str] = []
    user_prompts_seen: list[str] = []

    original_call_llm = HandlerGenerationConsumer._call_llm  # type: ignore[attr-defined]

    async def _patched_call_llm(
        self: Any,
        task_description: str,
        attempt: int,
        previous_errors: list[str] | None = None,
        context_pack: str = "",
    ) -> tuple[str, int, int, EnumUsageSource]:
        # Replicate exactly what the real _call_llm does before the LLM call,
        # then capture the post-apply_inference_protocol prompts.
        user_content = f"Task: {task_description}"
        if attempt > 1 and previous_errors:
            error_list = "\n".join(f"- {e}" for e in previous_errors)
            user_content += (
                f"\n\nPrevious attempt failed with:\n{error_list}\nPlease fix them."
            )
        if context_pack:
            user_content = f"Context:\n{context_pack}\n\n{user_content}"

        from omnimarket.inference.protocol_config import apply_inference_protocol
        from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
            _DEFAULT_SYSTEM_PROMPT,
        )

        sys_p, usr_p, _ = apply_inference_protocol(
            system_prompt=_DEFAULT_SYSTEM_PROMPT,
            prompt=user_content,
            model=self._served_model_id,
            task_type="node_generation",
        )
        system_prompts_seen.append(sys_p)
        user_prompts_seen.append(usr_p)
        return _VALID_LLM_RESPONSE, 10, 20, EnumUsageSource.MEASURED

    HandlerGenerationConsumer._call_llm = _patched_call_llm  # type: ignore[method-assign]
    try:
        handler = _make_handler([_VALID_LLM_RESPONSE])
        await handler.handle(
            ModelNodeGenerationRequest(
                task_description="Build a stub node",
                correlation_id="corr-protocol-1",
            )
        )
    finally:
        HandlerGenerationConsumer._call_llm = original_call_llm  # type: ignore[method-assign]

    assert len(system_prompts_seen) == 1
    assert len(user_prompts_seen) == 1

    # Exemplar injected into system prompt for Qwen.
    assert "```yaml" in system_prompts_seen[0], (
        "After apply_inference_protocol, the system prompt must contain the ```yaml exemplar. "
        f"Got: {system_prompts_seen[0][:200]!r}"
    )
    # /no_think prepended to user prompt for Qwen.
    assert user_prompts_seen[0].lstrip().startswith("/no_think"), (
        "After apply_inference_protocol, the user prompt must start with /no_think for Qwen. "
        f"Got: {user_prompts_seen[0][:80]!r}"
    )


# ---------------------------------------------------------------------------
# End-to-end: handler passes when LLM returns well-formed response
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_contract_passed_on_well_formed_response() -> None:
    """contract_passed=True + non-empty contract_yaml + non-empty handler_source."""
    published: list[tuple[str, bytes]] = []
    handler = _make_handler([_VALID_LLM_RESPONSE], published=published)

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-e2e-omn12813-1",
        )
    )

    assert result.contract_passed is True, (
        f"contract_passed must be True for a well-formed LLM response. "
        f"Attempts: {[a.validation_errors for a in result.attempts]}"
    )
    assert result.contract_yaml.strip(), (
        "contract_yaml must be non-empty when contract_passed is True"
    )
    assert result.handler_source.strip(), (
        "handler_source must be non-empty when contract_passed is True"
    )
