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
import time
from datetime import UTC, datetime
from typing import Any

from omnimarket.events.provider_quota import EnumProviderQuotaSource
from omnimarket.inference import provider_quota_state
from omnimarket.inference.adapter_inference_bridge import (
    ModelInferenceAdapter,
    ModelInferenceJsonObjectResponseFormat,
)
from omnimarket.inference.protocol_config import ModelInferenceProtocolSelection
from omnimarket.inference.provider_quota_observation import (
    EmitEffectQuotaObservationSink,
    ProtocolProviderQuotaObservationSink,
    build_quota_observation,
    observe_failed_call,
)
from omnimarket.inference.provider_quota_state import (
    ProtocolProviderQuotaReader,
    quota_block_for_backend,
    read_provider_quota_snapshot,
)
from omnimarket.inference.secret_store_resolver import (
    api_key_ref_available,
    resolve_api_key,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
    resolve_delegation_backend,
    resolve_timeout_seconds,
)

logger = logging.getLogger(__name__)

# OMN-17427: Gemini is reserved for explicitly pinned operator tests. Ordinary
# judging uses the existing free OpenRouter reasoning rung, declared by the
# routing contract with the judge_adequacy capability. Local reviewer fallback
# below remains available when that provider's key is unbound.
_DEFAULT_JUDGE_BACKEND_ID = "openrouter-nemotron-super"

# OMN-19198: the reviewer a machine can bind with nothing but its own model.
# The declared judge above names a metered provider and a credential reference;
# a customer's machine that holds no such credential used to leave the reviewer
# leg pointing at an endpoint it could never authenticate to. When the declared
# judge's credential is not available HERE, the reviewer leg moves to the first
# of these local rungs the machine has bound. They are the rungs the customer's
# own overlay binds (the shipped contract declares them with no endpoint), and
# a fallback candidate that itself declares a credential this machine cannot
# resolve is skipped, so the fallback never binds a credential either.
_LOCAL_REVIEWER_BACKEND_IDS: tuple[str, ...] = (
    "local-heavy-reasoning",
    "local-coder",
)


class JudgeReviewerUnboundError(RuntimeError):
    """No reviewer is bindable on this machine: the declared judge's credential
    is not available and no local rung has an endpoint (OMN-19198)."""


class RoutingResolvedJudgeInferenceAdapter(ModelInferenceAdapter):
    """Judge inference adapter resolving a CONCRETE backend via routing authority.

    Resolves ``model_id`` + verbatim ``endpoint_ref`` + ``secret_ref`` for the
    pinned judge backend from the routing contract, resolves the API key at the
    effect boundary, and posts the chat completion VERBATIM via the canonical
    delegation transport. Never passes a tier name to the inference layer.
    """

    def __init__(
        self,
        *,
        backend_id: str = _DEFAULT_JUDGE_BACKEND_ID,
        quota_reader: ProtocolProviderQuotaReader | None = None,
        observation_sink: ProtocolProviderQuotaObservationSink | None = None,
    ) -> None:
        self._backend_id = backend_id
        # OMN-20154: quota state is read from the durable projection and
        # written as observations; the adapter keeps none of it in memory.
        # Resolved lazily so construction does no I/O.
        self._quota_reader = quota_reader
        self._observation_sink = observation_sink

    def _reader(self) -> ProtocolProviderQuotaReader | None:
        if self._quota_reader is None:
            self._quota_reader = provider_quota_state.resolve_provider_quota_reader()
        return self._quota_reader

    def _sink(self) -> ProtocolProviderQuotaObservationSink:
        if self._observation_sink is None:
            self._observation_sink = EmitEffectQuotaObservationSink()
        return self._observation_sink

    def _resolve_backend(self) -> ModelResolvedDelegationBackend:
        """Resolve the reviewer this machine can actually call.

        The declared judge when its credential is available here (or it needs
        none) -- the lab, and any customer who supplied that provider's key.
        Otherwise the first bound local rung (OMN-19198): the customer's own
        model, no credential. Otherwise :class:`JudgeReviewerUnboundError`,
        which the adequacy handler turns into a typed ``JUDGE_NO_REVIEWER_BOUND``
        verdict without calling anything.
        """
        # task_type is unused when backend_id pins the backend, but the resolver
        # signature requires it; pass the judge task class for provenance.
        declared_failure: str
        try:
            declared = resolve_delegation_backend(
                "judge_adequacy", backend_id=self._backend_id
            )
        except RuntimeError as exc:
            declared_failure = (
                f"declared judge {self._backend_id!r} unresolvable: {exc}"
            )
        else:
            if api_key_ref_available(
                declared.secret_ref, env_var_fallback=declared.api_key_env
            ):
                return declared
            # Names the backend only: the reference name is not a secret, but
            # nothing about it helps a reader and it does not belong in a log.
            declared_failure = (
                f"declared judge {self._backend_id!r} has no provider key "
                "this machine can resolve"
            )
        for local_id in _LOCAL_REVIEWER_BACKEND_IDS:
            try:
                local = resolve_delegation_backend(
                    "judge_adequacy", backend_id=local_id
                )
            except RuntimeError:
                continue
            if local.secret_ref is not None and not api_key_ref_available(
                local.secret_ref, env_var_fallback=local.api_key_env
            ):
                continue
            logger.info(
                "judge reviewer leg on local rung %s (%s); %s (OMN-19198)",
                local.backend_id,
                local.model_id,
                declared_failure,
            )
            return local
        raise JudgeReviewerUnboundError(
            f"no reviewer is bound on this machine: {declared_failure}, and no "
            f"local rung ({', '.join(_LOCAL_REVIEWER_BACKEND_IDS)}) has an endpoint"
        )

    def reviewer_unbound_reason(self) -> str | None:
        """Why no reviewer can be called here, or None when one can (OMN-19198)."""
        try:
            self._resolve_backend()
        except JudgeReviewerUnboundError as exc:
            return str(exc)
        return None

    def quota_disabled(self) -> bool:
        """Return whether the judge's quota key is blocked in the projection.

        OMN-16932 put this check in front of the call: one judge call per
        delegation against a 20-request free tier exhausted the lane in ~10
        delegations, and every later delegation spent a guaranteed 429 to
        rediscover the cap. OMN-20154 moves the state it reads from process
        memory to the durable ``provider_quota_state`` projection, read under
        the lane's tenant. An unreadable projection withholds a metered judge
        (fail closed); a local reviewer rung is never withheld by it.

        Resolution failures are swallowed to ``False``: a judge that cannot
        resolve its own backend must still attempt the call and fail closed to
        ``JUDGE_FAILED`` through the existing path, never be silently skipped
        on an unrelated error.
        """
        try:
            backend = self._resolve_backend()
        except Exception:  # pragma: no cover - resolution errors surface on call
            return False
        snapshot = read_provider_quota_snapshot(self._reader(), tenant_id=None)
        block = quota_block_for_backend(
            snapshot,
            endpoint_url=backend.endpoint_ref,
            api_key_ref=backend.secret_ref,
            model_name=backend.model_id,
        )
        if block is not None:
            logger.info(
                "judge withheld by quota state: provider=%s scope=%s until=%s (%s)",
                block.provider_id,
                block.model_scope,
                block.blocked_until,
                block.disposition,
            )
        return block is not None

    def record_quota_failure(
        self,
        *,
        backend: ModelResolvedDelegationBackend,
        error: object,
        latency_ms: int = 0,
    ) -> None:
        """Emit the observation for a failed judge call.

        The judge is usually the FIRST metered call of a delegation, so it is
        usually the one that discovers an exhausted quota; the observation it
        emits is what lets the next routing decision skip that key.

        ``error`` is duck-typed rather than annotated as a concrete transport
        exception: this module lives inside a REDUCER node, where ARCH-002
        forbids importing a transport library at runtime. Reading
        ``error.response`` structurally keeps the reducer transport-agnostic.
        An error with no HTTP response (a timeout, a refused connection) is
        still a call and is observed as one.
        """
        response: Any = getattr(error, "response", None)
        status = (
            getattr(response, "status_code", None) if response is not None else None
        )
        body: object = None
        headers: dict[str, str] = {}
        if response is not None:
            json_reader = getattr(response, "json", None)
            if callable(json_reader):
                try:
                    body = json_reader()
                except Exception:  # pragma: no cover - non-JSON error bodies
                    body = None
            raw_headers = getattr(response, "headers", None)
            if raw_headers is not None:
                try:
                    headers = {str(k): str(v) for k, v in dict(raw_headers).items()}
                except Exception:  # pragma: no cover - exotic header objects
                    headers = {}
        observation, verdict = observe_failed_call(
            tenant_id=None,
            endpoint_url=backend.endpoint_ref,
            api_key_ref=backend.secret_ref,
            model_name=backend.model_id,
            error_message=str(error),
            observed_at=datetime.now(UTC),
            latency_ms=latency_ms,
            source=EnumProviderQuotaSource.JUDGE,
            http_status=status if isinstance(status, int) else None,
            body=body if isinstance(body, dict) else None,
            headers=headers or None,
        )
        if observation is not None:
            self._sink().emit(observation)
        if verdict is not None and not verdict.retryable:
            logger.warning(
                "judge_quota_block provider=%s code=%s until=%s: %s",
                verdict.provider_id,
                verdict.provider_code,
                verdict.disabled_until,
                verdict.reason,
            )

    def _record_quota_success(
        self, *, backend: ModelResolvedDelegationBackend, latency_ms: int
    ) -> None:
        observation = build_quota_observation(
            tenant_id=None,
            endpoint_url=backend.endpoint_ref,
            api_key_ref=backend.secret_ref,
            model_name=backend.model_id,
            succeeded=True,
            observed_at=datetime.now(UTC),
            latency_ms=latency_ms,
            source=EnumProviderQuotaSource.JUDGE,
            http_status=200,
        )
        if observation is not None:
            self._sink().emit(observation)

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
        response_format: ModelInferenceJsonObjectResponseFormat | None = None,
        protocol_selection: ModelInferenceProtocolSelection | None = None,
    ) -> str:
        if protocol_selection is not None:
            raise ValueError(
                "RoutingResolvedJudgeInferenceAdapter does not support provider "
                "protocol selection"
            )
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
            response_format,
        )

    def _infer_sync(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None,
        response_format: ModelInferenceJsonObjectResponseFormat | None,
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
        if response_format is not None:
            payload["response_format"] = response_format.model_dump(mode="json")

        # The caller's timeout is the judge timeout budget; honour the smaller of
        # the caller's value and the backend's contract-resolved ceiling so the
        # judge is never capped above the routing contract's per-backend timeout.
        backend_timeout = resolve_timeout_seconds(backend_timeout_ms=backend.timeout_ms)
        effective_timeout = min(timeout_seconds, backend_timeout)

        started = time.monotonic()
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
            self.record_quota_failure(
                backend=backend,
                error=exc,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        self._record_quota_success(
            backend=backend, latency_ms=int((time.monotonic() - started) * 1000)
        )
        return str(response.json_body["choices"][0]["message"]["content"])


__all__ = ["JudgeReviewerUnboundError", "RoutingResolvedJudgeInferenceAdapter"]
