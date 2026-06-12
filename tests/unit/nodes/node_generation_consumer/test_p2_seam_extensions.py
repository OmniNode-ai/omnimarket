# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for OMN-12794 P2-1 SEAM: additive emitter-first model extensions.

Coverage:
- ModelNodeGenerationRequest carries context_pack / context_artifacts / context_pack_hash
- ModelGenerationBenchmark carries prompt_tokens, completion_tokens, first_pass_success,
  context_pack_hash  (all sourced from typed fields, not log scraping)
- HandlerGenerationConsumer prepends context_pack to the user prompt
- first_pass_success is True only when attempts[0].contract_passed
- context_pack_hash is echoed from command to benchmark
- prompt_tokens / completion_tokens are the per-attempt sums
- Existing callers that omit context fields continue to work (additive, no break)
- EnumFailureStage and ModelAttemptReductionRow are well-formed
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_proof_class import EnumProofClass
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
)
from omnimarket.nodes.node_generation_consumer.models.model_attempt_reduction import (
    EnumFailureStage,
    ModelAttemptReductionRow,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelContextArtifact,
    ModelGenerationBenchmark,
    ModelNodeGenerationRequest,
)

# ---------------------------------------------------------------------------
# Shared test fixtures — identical valid LLM response used across handler tests
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
    "Here is your node:\n"
    "```yaml\n" + _VALID_CONTRACT_YAML + "```\n\n"
    "```python\n" + _VALID_HANDLER_SOURCE + "```\n"
)

_INVALID_CONTRACT_YAML = "not_a_mapping: [broken"
_INVALID_LLM_RESPONSE = (
    "```yaml\n" + _INVALID_CONTRACT_YAML + "\n```\n\n"
    "```python\n" + _VALID_HANDLER_SOURCE + "```\n"
)


class _FakeUsage:
    def __init__(self, inp: int = 10, out: int = 20, usage_source: str = "api") -> None:
        self.tokens_input = inp
        self.tokens_output = out
        self.tokens_total = inp + out
        # OMN-12996: provider-reported provenance ("api" -> MEASURED).
        self.usage_source = usage_source


class _FakeResponse:
    def __init__(self, text: str, inp: int = 10, out: int = 20) -> None:
        self.generated_text = text
        self.usage = _FakeUsage(inp, out)
        self.latency_ms = 100.0


class _CapturingEffect:
    """Records every user_content string seen across calls."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        # Each call records the user_content delivered to the LLM.
        # Because the fake effect receives None (injected path skips request
        # building), we capture the prompt by monkey-patching _call_llm in the
        # test instead.  This class just drives responses deterministically.
        self.call_count = 0

    async def handle(self, request: Any) -> _FakeResponse:
        await asyncio.sleep(0)
        self.call_count += 1
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
        effect_handler=_CapturingEffect(responses),
        event_publisher=_publisher,
    )


@pytest.fixture(autouse=True)
def _isolate_onex_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Isolate the replay-state dir per test (OMN-12996).

    The handler persists a replay benchmark keyed by correlation_id under
    ONEX_STATE_DIR; when the operator's shared state dir leaks into the test
    environment, handle() short-circuits on a stale marker (possibly written by
    older code) instead of recomputing. Point both state-root env keys at a
    per-test tmp dir so every run is hermetic.
    """
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "onex_state"))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)


# ---------------------------------------------------------------------------
# ModelNodeGenerationRequest — context seam fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_request_accepts_context_pack_fields() -> None:
    """ModelNodeGenerationRequest accepts all P2-1 context fields."""
    artifact = ModelContextArtifact(
        factor="golden_chain",
        content="some chain content",
        source_ref="chain:abc123",
        content_hash="sha256:deadbeef",
    )
    req = ModelNodeGenerationRequest(
        task_description="Build a stub node",
        correlation_id="corr-ctx-1",
        context_pack="Context preamble for generation.",
        context_artifacts=[artifact],
        context_pack_hash="sha256:aabbcc",
    )
    assert req.context_pack == "Context preamble for generation."
    assert len(req.context_artifacts) == 1
    assert req.context_artifacts[0].factor == "golden_chain"
    assert req.context_pack_hash == "sha256:aabbcc"


@pytest.mark.unit
def test_request_context_fields_default_to_empty() -> None:
    """Existing callers that omit context fields get safe empty defaults (additive)."""
    req = ModelNodeGenerationRequest(
        task_description="Build a stub node",
        correlation_id="corr-default-1",
    )
    assert req.context_pack == ""
    assert req.context_artifacts == []
    assert req.context_pack_hash == ""


@pytest.mark.unit
def test_request_is_frozen() -> None:
    """ModelNodeGenerationRequest must be immutable (frozen=True)."""
    req = ModelNodeGenerationRequest(
        task_description="Build a stub node",
        correlation_id="corr-frozen-1",
    )
    with pytest.raises(ValidationError):
        req.context_pack = "mutate"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ModelGenerationBenchmark — P2-1 event fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_benchmark_accepts_p2_fields() -> None:
    """ModelGenerationBenchmark accepts all P2-1 event extension fields."""
    bench = ModelGenerationBenchmark(
        correlation_id="corr-bench-1",
        task_description="stub",
        prompt_tokens=150,
        completion_tokens=80,
        first_pass_success=True,
        context_pack_hash="sha256:cafebabe",
    )
    assert bench.prompt_tokens == 150
    assert bench.completion_tokens == 80
    assert bench.first_pass_success is True
    assert bench.context_pack_hash == "sha256:cafebabe"


@pytest.mark.unit
def test_benchmark_p2_fields_default_to_safe_values() -> None:
    """Existing event payloads missing P2 fields deserialise with safe defaults."""
    bench = ModelGenerationBenchmark(
        correlation_id="corr-compat-1",
        task_description="stub",
    )
    assert bench.prompt_tokens == 0
    assert bench.completion_tokens == 0
    assert bench.first_pass_success is False
    assert bench.context_pack_hash == ""


# ---------------------------------------------------------------------------
# HandlerGenerationConsumer — context_pack injected into prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_prepends_context_pack_to_prompt() -> None:
    """When context_pack is set, it must reach the LLM prompt before the task text."""
    prompt_seen: list[str] = []

    class _ProbingEffect:
        async def handle(self, request: Any) -> _FakeResponse:
            await asyncio.sleep(0)
            # The injected-effect path passes None as request; capture via closure.
            return _FakeResponse(_VALID_LLM_RESPONSE)

    original_call_llm = HandlerGenerationConsumer._call_llm  # type: ignore[attr-defined]

    async def _patched_call_llm(
        self: Any,
        task_description: str,
        attempt: int,
        previous_errors: list[str] | None = None,
        context_pack: str = "",
    ) -> tuple[str, int, int, EnumUsageSource]:
        # Build expected user_content the same way the handler does.
        user_content = f"Task: {task_description}"
        if context_pack:
            user_content = f"Context:\n{context_pack}\n\n{user_content}"
        prompt_seen.append(user_content)
        return _VALID_LLM_RESPONSE, 10, 20, EnumUsageSource.MEASURED

    HandlerGenerationConsumer._call_llm = _patched_call_llm  # type: ignore[method-assign]
    try:
        handler = _make_handler([_VALID_LLM_RESPONSE])
        await handler.handle(
            ModelNodeGenerationRequest(
                task_description="Build a stub node",
                correlation_id="corr-ctx-prompt-1",
                context_pack="Golden chain preamble.",
            )
        )
    finally:
        HandlerGenerationConsumer._call_llm = original_call_llm  # type: ignore[method-assign]

    assert len(prompt_seen) == 1
    assert prompt_seen[0].startswith("Context:\nGolden chain preamble.")
    assert "Task: Build a stub node" in prompt_seen[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_no_context_pack_omits_context_prefix() -> None:
    """When context_pack is empty, the prompt must not contain a Context: preamble."""
    prompt_seen: list[str] = []

    original_call_llm = HandlerGenerationConsumer._call_llm  # type: ignore[attr-defined]

    async def _patched_call_llm(
        self: Any,
        task_description: str,
        attempt: int,
        previous_errors: list[str] | None = None,
        context_pack: str = "",
    ) -> tuple[str, int, int, EnumUsageSource]:
        user_content = f"Task: {task_description}"
        if context_pack:
            user_content = f"Context:\n{context_pack}\n\n{user_content}"
        prompt_seen.append(user_content)
        return _VALID_LLM_RESPONSE, 10, 20, EnumUsageSource.MEASURED

    HandlerGenerationConsumer._call_llm = _patched_call_llm  # type: ignore[method-assign]
    try:
        handler = _make_handler([_VALID_LLM_RESPONSE])
        await handler.handle(
            ModelNodeGenerationRequest(
                task_description="Build a stub node",
                correlation_id="corr-no-ctx-1",
                # context_pack intentionally omitted — default ""
            )
        )
    finally:
        HandlerGenerationConsumer._call_llm = original_call_llm  # type: ignore[method-assign]

    assert len(prompt_seen) == 1
    assert "Context:" not in prompt_seen[0]


# ---------------------------------------------------------------------------
# HandlerGenerationConsumer — P2-1 benchmark event fields emitted correctly
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_benchmark_emits_prompt_completion_token_split() -> None:
    """Emitted benchmark must carry prompt_tokens and completion_tokens as sums."""
    # FakeUsage returns inp=10, out=20 per attempt.
    published: list[tuple[str, bytes]] = []
    handler = _make_handler([_VALID_LLM_RESPONSE], published=published)

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-tokens-1",
        )
    )

    # One attempt, fake returns 10 input / 20 output.
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 20


@pytest.mark.unit
@pytest.mark.asyncio
async def test_benchmark_token_split_sums_all_attempts() -> None:
    """Token counts must sum across all attempts (retry path)."""
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(
        [_INVALID_LLM_RESPONSE, _VALID_LLM_RESPONSE], published=published
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-tokens-retry-1",
            max_attempts=2,
        )
    )

    # Two attempts, each returning 10/20 from FakeUsage.
    assert result.prompt_tokens == 20
    assert result.completion_tokens == 40


@pytest.mark.unit
@pytest.mark.asyncio
async def test_benchmark_first_pass_success_true_when_attempt_one_passes() -> None:
    """first_pass_success must be True when the first attempt validates."""
    handler = _make_handler([_VALID_LLM_RESPONSE])

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-fpass-1",
        )
    )

    assert result.first_pass_success is True
    assert result.attempt_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_benchmark_first_pass_success_false_when_first_attempt_fails() -> None:
    """first_pass_success must be False when the first attempt fails."""
    handler = _make_handler([_INVALID_LLM_RESPONSE, _VALID_LLM_RESPONSE])

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-fpass-2",
            max_attempts=2,
        )
    )

    assert result.first_pass_success is False
    assert result.contract_passed is True  # final succeeded
    assert result.attempt_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_benchmark_first_pass_success_false_when_all_attempts_fail() -> None:
    """first_pass_success must be False when all attempts fail."""
    handler = _make_handler([_INVALID_LLM_RESPONSE, _INVALID_LLM_RESPONSE])

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-fpass-3",
            max_attempts=2,
        )
    )

    assert result.first_pass_success is False
    assert result.contract_passed is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_benchmark_echoes_context_pack_hash() -> None:
    """Emitted benchmark must echo the command's context_pack_hash."""
    pack_hash = "sha256:" + hashlib.sha256(b"some context text").hexdigest()
    handler = _make_handler([_VALID_LLM_RESPONSE])

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-hash-echo-1",
            context_pack="some context text",
            context_pack_hash=pack_hash,
        )
    )

    assert result.context_pack_hash == pack_hash


@pytest.mark.unit
@pytest.mark.asyncio
async def test_benchmark_context_pack_hash_empty_when_no_pack() -> None:
    """When no context_pack is supplied, context_pack_hash must be empty."""
    handler = _make_handler([_VALID_LLM_RESPONSE])

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-hash-empty-1",
        )
    )

    assert result.context_pack_hash == ""


# ---------------------------------------------------------------------------
# EnumFailureStage — all values present and string-typed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enum_failure_stage_has_required_values() -> None:
    """EnumFailureStage must declare all stages specified in P2-1."""
    required = {
        "none",
        "pack_build",
        "budget_fail",
        "generation",
        "validation",
        "downstream_gate",
    }
    actual = {s.value for s in EnumFailureStage}
    assert required == actual, (
        f"Missing or extra stages: {required.symmetric_difference(actual)}"
    )


@pytest.mark.unit
def test_enum_failure_stage_is_str_enum() -> None:
    """EnumFailureStage values must compare equal to their string equivalents."""
    assert EnumFailureStage.NONE == "none"
    assert EnumFailureStage.PACK_BUILD == "pack_build"
    assert EnumFailureStage.BUDGET_FAIL == "budget_fail"
    assert EnumFailureStage.DOWNSTREAM_GATE == "downstream_gate"


# ---------------------------------------------------------------------------
# ModelAttemptReductionRow — shape and constraints
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_attempt_reduction_row_accepts_full_payload() -> None:
    """ModelAttemptReductionRow must accept a fully-populated row."""
    row = ModelAttemptReductionRow(
        run_id="run-001",
        correlation_id="corr-row-1",
        task_id="task_001",
        context_factor_subset="golden_exemplar",
        context_pack_hash="sha256:abcd1234",
        attempt_count=2,
        first_pass_success=False,
        final_success=True,
        failure_stage=EnumFailureStage.NONE,
        prompt_tokens=450,
        completion_tokens=120,
        estimated_cost=0.0,
        model_id="Qwen3.6-35B-A3B",
        provider="local",
        endpoint_ref="local-coder",
        proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
    )
    assert row.run_id == "run-001"
    assert row.first_pass_success is False
    assert row.final_success is True
    assert row.failure_stage == EnumFailureStage.NONE
    assert row.proof_class == EnumProofClass.RUNTIME_OBSERVED_ONLY


@pytest.mark.unit
def test_attempt_reduction_row_safe_defaults() -> None:
    """ModelAttemptReductionRow must be constructable with only required fields."""
    row = ModelAttemptReductionRow(
        run_id="run-002",
        correlation_id="corr-row-2",
        task_id="task_002",
    )
    assert row.context_factor_subset == "off"
    assert row.context_pack_hash == ""
    assert row.attempt_count == 0
    assert row.first_pass_success is False
    assert row.final_success is False
    assert row.failure_stage == EnumFailureStage.NONE
    assert row.prompt_tokens == 0
    assert row.completion_tokens == 0
    assert row.estimated_cost == 0.0
    assert row.proof_class == EnumProofClass.RUNTIME_OBSERVED_ONLY


@pytest.mark.unit
def test_attempt_reduction_row_is_frozen() -> None:
    """ModelAttemptReductionRow must be immutable (frozen=True)."""
    row = ModelAttemptReductionRow(
        run_id="run-003",
        correlation_id="corr-row-3",
        task_id="task_003",
    )
    with pytest.raises(ValidationError):
        row.attempt_count = 99  # type: ignore[misc]


@pytest.mark.unit
def test_attempt_reduction_row_off_arm_has_empty_context_hash() -> None:
    """Off-arm rows must carry an empty context_pack_hash (no pack injected)."""
    row = ModelAttemptReductionRow(
        run_id="run-004",
        correlation_id="corr-row-4",
        task_id="task_004",
        context_factor_subset="off",
        # context_pack_hash intentionally omitted — default ""
    )
    assert row.context_pack_hash == ""


# ---------------------------------------------------------------------------
# Contract coherence — production contract.yaml declares new fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_production_contract_declares_context_pack_input() -> None:
    """contract.yaml must declare context_pack, context_artifacts, context_pack_hash as inputs."""
    import yaml

    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        _CONTRACT_PATH,
    )

    with open(_CONTRACT_PATH) as f:
        contract = yaml.safe_load(f)

    inputs = contract.get("inputs", {})
    assert "context_pack" in inputs, "contract.yaml missing input: context_pack"
    assert "context_artifacts" in inputs, (
        "contract.yaml missing input: context_artifacts"
    )
    assert "context_pack_hash" in inputs, (
        "contract.yaml missing input: context_pack_hash"
    )

    # All three must be optional (not required)
    for field in ("context_pack", "context_artifacts", "context_pack_hash"):
        assert inputs[field].get("required", True) is False, (
            f"contract.yaml input '{field}' must be optional (required: false)"
        )


@pytest.mark.unit
def test_production_contract_declares_p2_outputs() -> None:
    """contract.yaml must declare prompt_tokens, completion_tokens, first_pass_success outputs."""
    import yaml

    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        _CONTRACT_PATH,
    )

    with open(_CONTRACT_PATH) as f:
        contract = yaml.safe_load(f)

    outputs = contract.get("outputs", {})
    required_outputs = {
        "prompt_tokens",
        "completion_tokens",
        "first_pass_success",
        "context_pack_hash",
    }
    missing = required_outputs - set(outputs.keys())
    assert not missing, f"contract.yaml outputs missing P2-1 fields: {missing}"


# ---------------------------------------------------------------------------
# EnumUsageSource — usage_source is a typed enum, not a bare string
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_benchmark_usage_source_is_enum() -> None:
    """ModelGenerationBenchmark.usage_source must be EnumUsageSource, not a bare str."""
    bench = ModelGenerationBenchmark(
        correlation_id="corr-usage-1",
        task_description="stub",
    )
    # OMN-12996: the default is now UNKNOWN (honest absent-provenance), not the
    # old hardcoded ESTIMATED. The emitter sets MEASURED/ESTIMATED only when the
    # provider/local tokenizer actually reported usage.
    assert isinstance(bench.usage_source, EnumUsageSource)
    assert bench.usage_source == EnumUsageSource.UNKNOWN


@pytest.mark.unit
def test_benchmark_usage_source_rejects_unknown_string() -> None:
    """ModelGenerationBenchmark must reject an unrecognised usage_source string."""
    with pytest.raises(ValidationError):
        ModelGenerationBenchmark(
            correlation_id="corr-usage-2",
            task_description="stub",
            usage_source="totally_unknown_value",  # type: ignore[arg-type]
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_emits_enum_usage_source() -> None:
    """Emitted benchmark carries a typed EnumUsageSource (not a bare str).

    OMN-12996: with the fake effect reporting a provider usage block
    (_FakeUsage.usage_source="api"), the honest provenance is MEASURED — the
    benchmark no longer hardcodes ESTIMATED.
    """
    handler = _make_handler([_VALID_LLM_RESPONSE])
    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-enum-usage-1",
        )
    )
    assert isinstance(result.usage_source, EnumUsageSource)
    assert result.usage_source == EnumUsageSource.MEASURED
