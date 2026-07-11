# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT handler: generate node source from a spec via an LLM (tier-4a).

An EFFECT — it owns the network I/O (the inference call). The inference adapter
is injectable (``ModelInferenceAdapter`` from the shared inference package), so
tests drive it deterministically without a live model; the default is the shared
``AdapterInferenceBridge``. The handler echoes the accumulating
``ModelCodegenPipelineState`` back with the generated source recorded, so the
orchestrator threads state through this hop for real.
"""

from __future__ import annotations

import re
from typing import Literal

from omnimarket.codegen.models import (
    ModelCodegenSpec,
    ModelLlmGenerateCommand,
    ModelLlmGenerateResult,
)
from omnimarket.inference.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
    ModelInferenceBridgeConfig,
)

_DEFAULT_MODEL_KEY = "codegen"
_DEFAULT_TIMEOUT_SECONDS = 120.0

_SYSTEM_PROMPT = (
    "You are an ONEX node code generator. Given a node spec, emit a single "
    "Python module implementing the node's handler class. Output only the "
    "Python source (optionally in one fenced code block); no prose."
)

# Extract the first fenced Python block when the model wraps its output in one.
_FENCE_RE = re.compile(r"```(?:python)?\n(?P<code>.*?)```", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """Return the code body, unwrapping a single fenced block if present."""
    match = _FENCE_RE.search(text)
    if match is not None:
        return match.group("code").strip() + "\n"
    return text.strip() + "\n"


def _build_prompt(spec: ModelCodegenSpec) -> str:
    """Build the user prompt describing the node to generate."""
    lines = [
        f"Generate an ONEX {spec.archetype} node.",
        f"Class name: {spec.node_name}",
        f"Base class: {spec.base_class}",
        f"Required handler method: {spec.handler_method}",
        f"Namespace: {spec.namespace}",
    ]
    if spec.description:
        lines.append(f"Description: {spec.description}")
    if spec.prompt_hint:
        lines.append(f"Additional guidance: {spec.prompt_hint}")
    return "\n".join(lines)


class HandlerLlmCodegen:
    """EFFECT: generate node source via an injectable inference adapter."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        inference: ModelInferenceAdapter | None = None,
        *,
        model_key: str = _DEFAULT_MODEL_KEY,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._inference = inference or AdapterInferenceBridge(
            ModelInferenceBridgeConfig()
        )
        self._model_key = model_key
        self._timeout_seconds = timeout_seconds

    async def handle(self, command: ModelLlmGenerateCommand) -> ModelLlmGenerateResult:
        spec = command.state.spec
        raw = await self._inference.infer(
            model_key=self._model_key,
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_build_prompt(spec),
            timeout_seconds=self._timeout_seconds,
        )
        source_text = _strip_code_fences(raw)
        return ModelLlmGenerateResult(state=command.state.with_source(source_text))


__all__ = ["HandlerLlmCodegen"]
