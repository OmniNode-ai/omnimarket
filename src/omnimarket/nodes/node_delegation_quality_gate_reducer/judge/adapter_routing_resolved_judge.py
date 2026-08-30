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
from typing import Any

from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter
from omnimarket.inference.provider_quota_policy import classify_quota_response
from omnimarket.inference.provider_quota_state import (
    quota_domain_disabled,
    record_quota_verdict,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
    resolve_delegation_backend,
    resolve_timeout_seconds,
)

logger = logging.getLogger(__name__)

# The judge rides a concrete CLOUD backend declared in the committed routing
# contract (bifrost_delegation.yaml). ``cloud-glm-judge`` carries a COMPLETE
# verbatim endpoint_url (z.ai GLM coding endpoint), a concrete model_name
# (glm-5.2, the FLAGSHIP), and the logical secret_ref ``llm.glm.api_key``. It is
# internet-reachable (not a LAN .201 backend), so the judge effect resolves it
# identically in every runtime lane.
#
# OMN-14225: the judge backend is DECOUPLED from the ``cloud-glm`` escalation
# backend. ``cloud-glm`` is the paid ESCALATION model and was repointed to the
# cheaper ``glm-5-turbo``; the JUDGE is a quality authority, not an escalation
# step, so it keeps its own ``cloud-glm-judge`` backend pinned to the flagship
# ``glm-5.2`` and is unaffected by the escalation model. To swap the judge model,
# repoint ``cloud-glm-judge``'s model_name in the contract — no code change here,
# and the escalation model stays independent.
_DEFAULT_JUDGE_BACKEND_ID = "cloud-glm-judge"


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

    def quota_disabled(self) -> bool:
        """Return whether the judge's provider is a known-exhausted quota domain.

        OMN-16932. ``cloud-glm-judge`` was repointed onto Gemini by OMN-14625
        (z.ai GLM is unreachable from ``.201``), which put the judge on the SAME
        free-tier counter as the ``cheap_cloud`` escalation rung. Against a cap
        of 20 requests, one judge call per delegation exhausts the lane in ~10
        delegations — and the judge kept calling afterwards, so every subsequent
        delegation spent a guaranteed-429 call to rediscover the same cap.

        Asking before calling turns that into a single 429 for the whole
        cooldown. Resolution failures are swallowed to ``False``: a judge that
        cannot resolve its own backend must still attempt the call and fail
        closed to ``JUDGE_FAILED`` through the existing path, never be silently
        skipped on an unrelated error.
        """
        try:
            endpoint = self._resolve_backend().endpoint_ref
        except Exception:  # pragma: no cover - resolution errors surface on call
            return False
        return quota_domain_disabled(endpoint) is not None

    def record_quota_failure(self, *, endpoint_url: str, error: object) -> None:
        """Fold a judge-leg 429 into the routing-visible quota ledger.

        The judge is the FIRST metered call in a delegation, so it is usually
        the one that discovers an exhausted quota. Recording it here is what
        lets the escalation target resolution (a different node, same process)
        know the provider is dead before it routes there — closing the loop that
        previously spent a second metered call per delegation to learn the same
        fact.

        ``error`` is duck-typed rather than annotated as a concrete transport
        exception: this module lives inside a REDUCER node, where ARCH-002
        forbids importing a transport library at runtime. Reading
        ``error.response.status_code`` / ``.json()`` structurally keeps the
        reducer transport-agnostic and works for any transport whose error
        carries the provider's response. Anything that does not carry one is
        simply not a quota signal and is ignored.
        """
        response: Any = getattr(error, "response", None)
        if response is None or getattr(response, "status_code", None) != 429:
            return
        body: object = None
        json_reader = getattr(response, "json", None)
        if callable(json_reader):
            try:
                body = json_reader()
            except Exception:  # pragma: no cover - non-JSON error bodies
                body = None
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=endpoint_url,
            body=body if isinstance(body, dict) else None,
        )
        if verdict is None:
            return
        record_quota_verdict(endpoint_url=endpoint_url, verdict=verdict)
        if not verdict.retryable:
            logger.warning(
                "judge_quota_disable provider=%s code=%s until=%s: %s",
                verdict.provider_id,
                verdict.provider_code,
                verdict.disabled_until,
                verdict.reason,
            )

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
            # OMN-13960: thread the backend's contract-declared ``api_key_env`` as
            # the env-var fallback (parity with the routing-availability check and
            # the delegation effect handler). The store-level provider-native alias
            # (OMN-13960) already covers the default GLM/OpenRouter/Gemini refs, but
            # passing ``api_key_env`` here keeps this call site consistent with the
            # other two and resolves a backend whose api_key_env is not in the
            # store-level alias map.
            resolved = resolve_api_key(
                backend.secret_ref, env_var_fallback=backend.api_key_env
            )
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

        try:
            response = transport.post_chat_completion(
                endpoint_url=backend.endpoint_ref,
                payload=payload,
                timeout_seconds=effective_timeout,
                extra_headers=headers,
            )
        except Exception as exc:
            # OMN-16932: a judge 429 is the lane's earliest quota signal. Record
            # it before re-raising so the escalation target resolution downstream
            # does not spend a second metered call rediscovering the same cap.
            # Caught broadly because ARCH-002 forbids naming a transport
            # exception type inside a reducer node; ``record_quota_failure``
            # ignores anything that does not carry a 429 response, so a timeout
            # or a connection error falls straight through. The raise is
            # unchanged — HandlerJudgeAdequacy still fails closed to
            # JUDGE_FAILED on every one of these.
            self.record_quota_failure(endpoint_url=backend.endpoint_ref, error=exc)
            raise
        return str(response.json_body["choices"][0]["message"]["content"])


__all__ = ["RoutingResolvedJudgeInferenceAdapter"]
