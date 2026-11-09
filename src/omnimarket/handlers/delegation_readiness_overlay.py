# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend overlay for one paid rung of a readiness leg (OMN-23182).

A probe-ladder rung may only name a loopback endpoint, so the one rung that must reach a real paid model keeps a loopback
placeholder URL in the ladder and this transport swaps in the real call at the effect seam: the leg runs through the
same ``HandlerDelegateSkill`` and ``LocalDelegationDispatchPort`` as every other leg, and for that rung only the
``ModelLlmDelegationCallRequest`` is rewritten (endpoint, model, output cap, credential header) before it reaches the
delegation effect. Every other rung is called unchanged.

The probe key is named by an environment variable; its value is read from the injected environment mapping when the
call is made, put in one request header and dropped. It is never returned, logged, put in an event or stored.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.delegation_readiness_probe import (
    ModelLegSpec,
    ProtocolLegTransport,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)

DelegationEffect = Callable[
    [ModelLlmDelegationCallRequest], ModelLlmDelegationCallResult
]


class ModelOpenRouterOverlay(BaseModel):
    """The contract settings of the real paid rung."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The environment variable that carries the probe key (a name, never the value).
    probe_key_env: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    max_tokens: int = Field(ge=1)
    #: The complete chat-completions URL the delegation effect posts to.
    base_url: str = Field(min_length=1, pattern=r"^https://")


def probe_key_present(
    environment: Mapping[str, str], overlay: ModelOpenRouterOverlay
) -> bool:
    """Whether the probe key variable holds a value; the value itself is not returned."""
    return bool(environment.get(overlay.probe_key_env, "").strip())


class OpenRouterOverlayTransport:
    """Wraps a leg transport; the rung named ``real_backend_id`` is called on OpenRouter instead of its placeholder."""

    def __init__(
        self,
        inner: ProtocolLegTransport,
        *,
        overlay: ModelOpenRouterOverlay,
        real_backend_id: str,
        environment: Mapping[str, str],
        effect: DelegationEffect | None = None,
    ) -> None:
        self._inner = inner
        self._overlay = overlay
        self._real_backend_id = real_backend_id
        self._environment = environment
        self._effect = effect
        self._real_url: str | None = None

    def endpoints(self, leg: ModelLegSpec) -> dict[str, str]:
        urls = self._inner.endpoints(leg)
        self._real_url = urls[self._real_backend_id]
        return urls

    def effect(self) -> DelegationEffect:
        inner = self._inner.effect()
        real_call = self._effect or _real_rung_call()

        def route(
            request: ModelLlmDelegationCallRequest,
        ) -> ModelLlmDelegationCallResult:
            if request.endpoint_ref != self._real_url:
                return (inner or _default_effect())(request)
            key = self._environment.get(self._overlay.probe_key_env, "").strip()
            rewritten = request.model_copy(
                update={
                    "endpoint_ref": self._overlay.base_url,
                    "model_id": self._overlay.model_id,
                    "max_tokens": self._overlay.max_tokens,
                    "provider": "openrouter",
                    "extra_headers": {
                        **request.extra_headers,
                        "Authorization": f"Bearer {key}",
                    },
                }
            )
            return real_call(rewritten)

        return route

    def virtual_elapsed_ms(self) -> int:
        return self._inner.virtual_elapsed_ms()

    def close(self) -> None:
        self._inner.close()


def _default_effect() -> DelegationEffect:
    """The production delegation effect, the one the loopback path calls."""
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
        HandlerLlmDelegationCall,
    )

    return HandlerLlmDelegationCall()


def _real_rung_call() -> DelegationEffect:
    """The delegation effect's own call and classification for the real rung, without its loopback ``/health`` probe.

    The effect probes ``<host>/health`` before it calls an endpoint; that is the loopback stand-in's contract and a hosted
    provider does not serve it, so the real rung enters the effect after the probe. The post, the failure classes and
    the cost and usage computation are the effect's own.
    """
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
        HandlerLlmDelegationCall,
    )

    handler = HandlerLlmDelegationCall()

    def call(request: ModelLlmDelegationCallRequest) -> ModelLlmDelegationCallResult:
        return handler._execute_call(  # noqa: SLF001 - the effect's post, classification and pricing without its /health probe
            request, request.endpoint_ref, None
        )

    return call
