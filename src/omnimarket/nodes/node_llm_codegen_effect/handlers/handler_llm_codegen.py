# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT handler: generate node source from a spec via an LLM (tier-4a).

An EFFECT — it owns the network I/O (the inference call). The inference adapter
is injectable (``ModelInferenceAdapter`` from the shared inference package), so
tests drive it deterministically without a live model. The default is the shared
``AdapterInferenceBridge`` configured with the one route this node uses, built
from the delegation routing contract's lab-hosted ``local-coder`` backend (the
routing authority), so a deployed node reaches the lab model and never a paid
provider; a contract that declares no endpoint or model for that backend fails
the call rather than falling back. The handler echoes the accumulating
``ModelCodegenPipelineState`` back with the generated source recorded, so the
orchestrator threads state through this hop for real.
"""

from __future__ import annotations

import re
from typing import Final, Literal

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
from omnimarket.inference.bridge_config_loader import resolve_bifrost_backend
from omnimarket.inference.protocol_config import ModelInferenceProtocolSelection

_DEFAULT_MODEL_KEY = "codegen"
_DEFAULT_TIMEOUT_SECONDS = 120.0
# The delegation routing contract's lab-hosted backend this node generates with.
_LAB_BACKEND_ID = "local-coder"
# The routing contract declares a complete request URL; the bridge appends this
# path to a base URL itself.
_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"

# The lab Qwen otherwise spends the bridge's whole completion budget thinking and
# returns an empty ``content``; the bridge then hands the reasoning text back as
# the module source. The profile turns the reasoning trace off for node generation.
_NO_THINK_PROTOCOL_SELECTION: Final[ModelInferenceProtocolSelection] = (
    ModelInferenceProtocolSelection(
        profile_id="local-qwen-generation-no-think",
        task_type="node_generation",
    )
)

_SYSTEM_PROMPT = (
    "You are an ONEX node code generator. Given a node spec, emit a single "
    "Python module implementing the node's handler class. Output only the "
    "Python source (optionally in one fenced code block); no prose. The module "
    "is type-checked on its own, so it must import nothing outside the Python "
    "standard library (define the spec's base class in the module itself as a "
    "docstring-only class that declares no methods), and every function must "
    "annotate its parameters and return type. The handler method must do the "
    "real work: a body that only raises, passes or returns a constant "
    "placeholder is rejected as a stub."
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


def build_lab_inference_adapter() -> ModelInferenceAdapter:
    """Build the default adapter from the routing contract's lab-hosted backend.

    Raises:
        RuntimeError: the contract declares no endpoint URL or no served model for
            the backend. There is deliberately no other provider to fall back to.
    """
    backend = resolve_bifrost_backend(_LAB_BACKEND_ID)
    endpoint = (backend.endpoint_url or "").strip() if backend else ""
    model_id = (backend.model_name or "").strip() if backend else ""
    if not endpoint or not model_id:
        msg = (
            f"Delegation routing backend {_LAB_BACKEND_ID!r} declares no "
            "endpoint URL or served model; the codegen LLM step has no lab "
            "model to call and does not fall back to another provider."
        )
        raise RuntimeError(msg)
    base_url = endpoint.rstrip("/").removesuffix(_CHAT_COMPLETIONS_SUFFIX)
    return AdapterInferenceBridge(
        ModelInferenceBridgeConfig(
            model_configs={
                _DEFAULT_MODEL_KEY: {
                    "base_url": base_url,
                    "model_id": model_id,
                    "transport": "http",
                    "timeout_seconds": _DEFAULT_TIMEOUT_SECONDS,
                }
            }
        )
    )


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
        # Built on first use: the runtime constructs this handler at wiring time,
        # before any model call, and the routing contract is read once per node.
        self._inference = inference
        self._model_key = model_key
        self._timeout_seconds = timeout_seconds

    async def handle(self, command: ModelLlmGenerateCommand) -> ModelLlmGenerateResult:
        spec = command.state.spec
        if self._inference is None:
            self._inference = build_lab_inference_adapter()
        raw = await self._inference.infer(
            model_key=self._model_key,
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_build_prompt(spec),
            timeout_seconds=self._timeout_seconds,
            protocol_selection=_NO_THINK_PROTOCOL_SELECTION,
        )
        source_text = _strip_code_fences(raw)
        return ModelLlmGenerateResult(state=command.state.with_source(source_text))


__all__ = ["HandlerLlmCodegen", "build_lab_inference_adapter"]
