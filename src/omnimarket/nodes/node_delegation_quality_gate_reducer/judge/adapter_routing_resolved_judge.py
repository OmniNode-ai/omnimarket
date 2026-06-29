# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Routing-resolved judge inference adapter (OMN-13470).

The LLM-judge adequacy effect needs a CONCRETE model + endpoint, never a tier
name. The default ``AdapterInferenceBridge`` is keyed by env-driven concrete
reviewer keys (``glm``, ``qwen3-coder``, ...) and historically the judge passed
the delegation TIER label ``cheap_cloud`` straight through, so the bridge raised
``ValueError: Unknown model_key: 'cheap_cloud'`` and every judge verdict came back
``judge_failed`` (OMN-13470).

This adapter resolves the judge backend through the SAME routing authority the
delegation call path uses (``resolve_delegation_backend``): a concrete
``model_id`` + the COMPLETE verbatim ``endpoint_ref`` + the logical
``secret_ref``, all from the committed routing contract + overlay. The literal
API key is resolved at the effect boundary via the canonical secret store
(``resolve_api_key``), and the request is posted VERBATIM through the same
transport (``post_chat_completion``) every delegation call uses. No tier name is
ever passed to the inference layer, no URL is constructed, and no env var is read
for the endpoint/model here.

``model_key`` on the :meth:`infer` signature is the resolved concrete model id
(provenance only) — the endpoint/model/key are resolved internally from the
routing authority, so the resolution can never silently accept a tier label.
"""

from __future__ import annotations

import asyncio
import logging

from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
    resolve_delegation_backend,
    resolve_timeout_seconds,
)

logger = logging.getLogger(__name__)

# The judge rides a concrete CLOUD backend declared in the committed routing
# contract (bifrost_delegation.yaml). ``cloud-glm`` carries a COMPLETE verbatim
# endpoint_url (z.ai GLM coding endpoint), a concrete model_name (glm-5.2), and
# the logical secret_ref ``llm.glm.api_key``. It is internet-reachable (not a LAN
# .201 backend), so the judge effect resolves it identically in every runtime
# lane. To swap the judge backend, repoint this id — no code change in the judge.
_DEFAULT_JUDGE_BACKEND_ID = "cloud-glm"


class RoutingResolvedJudgeInferenceAdapter(ModelInferenceAdapter):
    """Judge inference adapter resolving a CONCRETE backend via routing authority.

    Resolves ``model_id`` + verbatim ``endpoint_ref`` + ``secret_ref`` for the
    pinned judge backend from the routing contract, resolves the API key at the
    effect boundary, and posts the chat completion VERBATIM via the canonical
    delegation transport. Never passes a tier name to the inference layer.
    """

    def __init__(self, *, backend_id: str = _DEFAULT_JUDGE_BACKEND_ID) -> None:
        self._backend_id = backend_id

    def _resolve_backend(self) -> ModelResolvedDelegationBackend:
        # task_type is unused when backend_id pins the backend, but the resolver
        # signature requires it; pass the judge task class for provenance.
        return resolve_delegation_backend("judge_adequacy", backend_id=self._backend_id)

    def resolved_model_id(self) -> str:
        """Return the concrete model id resolved from the routing contract."""
        return self._resolve_backend().model_id

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        # Secret resolution (sync ProtocolSecretStore) and the blocking transport
        # both run off the event loop in one worker thread — the sync secret
        # resolver fails closed if called from inside a running loop (same pattern
        # as HandlerInferenceIntent.handle_async).
        return await asyncio.to_thread(
            self._infer_sync,
            system_prompt,
            user_prompt,
            timeout_seconds,
            temperature,
        )

    def _infer_sync(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None,
    ) -> str:
        backend = self._resolve_backend()

        api_key: str | None = None
        if backend.secret_ref is not None:
            resolved = resolve_api_key(backend.secret_ref)
            if resolved is None:
                raise ValueError(
                    f"Judge backend {backend.backend_id!r} declares secret_ref "
                    f"{backend.secret_ref!r} but it resolves to no value in the "
                    "secret store; the judge call fails closed rather than "
                    "calling the endpoint unauthenticated."
                )
            api_key = resolved.get_secret_value()

        headers: dict[str, str] = dict(backend.extra_headers)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload: dict[str, object] = {
            "model": backend.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": backend.max_tokens,
            "temperature": temperature if temperature is not None else 0.2,
        }

        # The caller's timeout is the judge timeout budget; honour the smaller of
        # the caller's value and the backend's contract-resolved ceiling so the
        # judge is never capped above the routing contract's per-backend timeout.
        backend_timeout = resolve_timeout_seconds(backend_timeout_ms=backend.timeout_ms)
        effective_timeout = min(timeout_seconds, backend_timeout)

        response = transport.post_chat_completion(
            endpoint_url=backend.endpoint_ref,
            payload=payload,
            timeout_seconds=effective_timeout,
            extra_headers=headers,
        )
        return str(response.json_body["choices"][0]["message"]["content"])


__all__ = ["RoutingResolvedJudgeInferenceAdapter"]
