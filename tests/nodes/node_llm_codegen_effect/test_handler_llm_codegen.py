# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_llm_codegen_effect — deterministic injected inference.

The EFFECT's network I/O is exercised through an injected ``ModelInferenceAdapter``
stub, so these run without a live model. They assert the generated source is
recorded on the pipeline state, that a fenced code block is unwrapped, and that
the prompt carries the spec's structural fields.
"""

from __future__ import annotations

import pytest

from omnimarket.codegen.models import (
    ModelCodegenPipelineState,
    ModelCodegenSpec,
    ModelLlmGenerateCommand,
)
from omnimarket.nodes.node_llm_codegen_effect.handlers.handler_llm_codegen import (
    HandlerLlmCodegen,
    _build_prompt,
    _strip_code_fences,
)


class _StubInference:
    """Deterministic inference double — duck-typed ``infer``, no ABC subclassing."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[tuple[str, str]] = []

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        self.calls.append((model_key, user_prompt))
        return self._response


def _command() -> ModelLlmGenerateCommand:
    spec = ModelCodegenSpec(
        node_name="NodeGreeterCompute",
        namespace="omninode.services.greeter.compute",
        archetype="compute",
        base_class="NodeCompute",
        handler_method="handle",
    )
    return ModelLlmGenerateCommand(state=ModelCodegenPipelineState(spec=spec))


@pytest.mark.asyncio
class TestGenerate:
    async def test_bare_response_is_recorded_on_state(self) -> None:
        stub = _StubInference("class NodeGreeterCompute:\n    pass\n")
        result = await HandlerLlmCodegen(stub).handle(_command())
        assert result.state.source_text == "class NodeGreeterCompute:\n    pass\n"
        # the spec is threaded through unchanged.
        assert result.state.spec.node_name == "NodeGreeterCompute"

    async def test_fenced_response_is_unwrapped(self) -> None:
        response = "sure:\n```python\nx = 1\n```\ntrailing prose\n"
        result = await HandlerLlmCodegen(_StubInference(response)).handle(_command())
        assert result.state.source_text == "x = 1\n"

    async def test_prompt_carries_structural_fields(self) -> None:
        stub = _StubInference("x = 1\n")
        await HandlerLlmCodegen(stub).handle(_command())
        _model_key, user_prompt = stub.calls[0]
        assert "NodeGreeterCompute" in user_prompt
        assert "NodeCompute" in user_prompt
        assert "handle" in user_prompt


@pytest.mark.unit
class TestHelpers:
    def test_strip_code_fences_prefers_fenced_block(self) -> None:
        assert _strip_code_fences("a\n```python\ncode\n```\nb") == "code\n"

    def test_strip_code_fences_without_fence_returns_stripped(self) -> None:
        assert _strip_code_fences("  code here  ") == "code here\n"

    def test_build_prompt_includes_optional_hint(self) -> None:
        spec = ModelCodegenSpec(
            node_name="NodeX",
            namespace="ns",
            archetype="effect",
            prompt_hint="use httpx",
        )
        assert "use httpx" in _build_prompt(spec)
