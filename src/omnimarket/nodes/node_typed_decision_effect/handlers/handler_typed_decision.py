# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_typed_decision_effect (OMN-19432).

EFFECT. Asks one typed question of the typed-decision backend the contract
pins, and returns the answer the caller should act on together with which
decider produced it.

Everything about the backend comes from the routing authority: the contract
names only a ``backend_id``; its endpoint, model, secret reference and timeout
resolve from ``bifrost_delegation.yaml`` plus overlay through
``resolve_delegation_backend``, and the key resolves at this boundary from the
secret store by that reference (an ``llm.<provider>.<name>`` reference
resolves from this machine's local credential store, never the environment).
No endpoint, model id or key literal lives in this module.

Order of operations, and why:

1. The work unit's repository must be public, resolved live. A missing
   attribution, a private or absent repository, and any resolution failure all
   refuse, and nothing is sent to the decision backend. This is the
   public-repository-only scoping the third-party call requires. The read
   carries a GitHub token when the secret store resolves one (anonymous reads
   are capped at 60 an hour), and a real 200 that reports the repository public
   is remembered for the contract's TTL. Only that positive answer is ever
   remembered, so a miss always falls through to a live read (OMN-20149).
2. The backend and its key must resolve; otherwise refuse.
3. One POST, verbatim, to the resolved endpoint. Any HTTP, transport or shape
   failure hands the decision to the optional incumbent, or returns no answer
   for a blind request without one.
4. A schema-valid answer below the contract's abstention threshold hands the
   decision to the optional incumbent, or returns no answer for a blind request.
   Only an answer at or above it is the model's.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import yaml
from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.config.service_endpoints import GITHUB_REST_URL
from omnimarket.inference.secret_store_resolver import (
    SecretResolutionError,
    resolve_api_key_loop_safe,
)
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_typed_decision_effect.handlers.visibility_cache import (
    VisibilityCache,
)
from omnimarket.nodes.node_typed_decision_effect.models.model_typed_decision import (
    EnumTypedDecisionDecider,
    EnumTypedDecisionKind,
    EnumTypedDecisionReason,
    ModelTypedDecisionRequest,
    ModelTypedDecisionResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
    resolve_delegation_backend,
    resolve_timeout_seconds,
)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
#: The key the model sees nothing of; answers come back under it.
_QUESTION_ID = "decision"
#: The routing task class a typed-decision backend declares as its capability.
_TASK_CLASS = "typed_decision"
_DETAIL_LIMIT = 300


class ModelTypedDecisionRouting(BaseModel):
    """The contract's ``decision_routing`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(..., min_length=1)
    abstain_below_probability: float = Field(..., ge=0.0, le=1.0)


class ModelWorkUnitScoping(BaseModel):
    """The contract's ``work_unit_scoping`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    require_public_repository: bool
    visibility_timeout_ms: int = Field(..., ge=100, le=60000)
    visibility_cache_ttl_seconds: int = Field(..., ge=0, le=86400)


def _load_contract_block(name: str) -> dict[str, Any]:
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get(name), dict):
        raise ValueError(f"{_CONTRACT_PATH} must declare a {name!r} mapping")
    block: dict[str, Any] = raw[name]
    return block


class HandlerTypedDecision:
    """EFFECT: one typed question to the contract-pinned decision backend."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        secret_store: ProtocolSecretStore | None = None,
        bifrost_config_path: Path | None = None,
        bifrost_overlay_path: Path | None = None,
        visibility_cache_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport = transport
        self._secret_store = secret_store
        self._bifrost_config_path = bifrost_config_path
        self._bifrost_overlay_path = bifrost_overlay_path
        self._routing = ModelTypedDecisionRouting.model_validate(
            _load_contract_block("decision_routing")
        )
        self._scoping = ModelWorkUnitScoping.model_validate(
            _load_contract_block("work_unit_scoping")
        )
        self._visibility_cache = VisibilityCache(
            ttl_seconds=self._scoping.visibility_cache_ttl_seconds,
            clock=clock,
            path=visibility_cache_path,
        )
        self._github_token: str | None = None
        self._github_token_resolved = False

    def handle(self, request: ModelTypedDecisionRequest) -> ModelTypedDecisionResult:
        refusal = self._refuse_unless_public(request)
        if refusal is not None:
            return refusal

        try:
            backend = resolve_delegation_backend(
                _TASK_CLASS,
                backend_id=self._routing.backend_id,
                config_path=self._bifrost_config_path,
                overlay_path=self._bifrost_overlay_path,
                store=self._secret_store,
            )
        except (RuntimeError, ValueError) as exc:
            return self._incumbent(
                request,
                EnumTypedDecisionDecider.INCUMBENT_REFUSED,
                EnumTypedDecisionReason.BACKEND_UNRESOLVED,
                detail=str(exc),
            )

        if backend.secret_ref is None:
            return self._incumbent(
                request,
                EnumTypedDecisionDecider.INCUMBENT_REFUSED,
                EnumTypedDecisionReason.CREDENTIAL_NOT_REGISTERED,
                backend=backend,
                detail="the routing contract declares no secret_ref for this backend",
            )
        try:
            # No env_var_fallback, deliberately: a provider key never comes
            # from the environment, whatever the backend declares.
            key = resolve_api_key_loop_safe(
                backend.secret_ref, store=self._secret_store, required=True
            )
        except SecretResolutionError as exc:
            return self._incumbent(
                request,
                EnumTypedDecisionDecider.INCUMBENT_REFUSED,
                EnumTypedDecisionReason.CREDENTIAL_NOT_REGISTERED,
                backend=backend,
                detail=str(exc),
            )
        if key is None:  # pragma: no cover - required=True raises instead
            return self._incumbent(
                request,
                EnumTypedDecisionDecider.INCUMBENT_REFUSED,
                EnumTypedDecisionReason.CREDENTIAL_NOT_REGISTERED,
                backend=backend,
            )

        body = self._request_body(request, backend)
        started = time.monotonic()
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=resolve_timeout_seconds(backend_timeout_ms=backend.timeout_ms),
            ) as client:
                response = client.post(
                    backend.endpoint_ref,
                    json=body,
                    headers={
                        **backend.extra_headers,
                        "Authorization": f"Bearer {key.get_secret_value()}",
                    },
                )
        except httpx.HTTPError as exc:
            return self._incumbent(
                request,
                EnumTypedDecisionDecider.INCUMBENT_BACKEND_ERROR,
                EnumTypedDecisionReason.BACKEND_TRANSPORT_ERROR,
                backend=backend,
                detail=type(exc).__name__,
            )
        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code != 200:
            return self._incumbent(
                request,
                EnumTypedDecisionDecider.INCUMBENT_BACKEND_ERROR,
                EnumTypedDecisionReason.BACKEND_HTTP_ERROR,
                backend=backend,
                http_status=response.status_code,
                latency_ms=latency_ms,
                detail=response.text[:_DETAIL_LIMIT],
            )
        return self._from_answer(request, backend, response, latency_ms)

    # ------------------------------------------------------------------ scoping

    def _refuse_unless_public(
        self, request: ModelTypedDecisionRequest
    ) -> ModelTypedDecisionResult | None:
        if not self._scoping.require_public_repository:
            return None
        if request.work_unit_repository is None:
            return self._incumbent(
                request,
                EnumTypedDecisionDecider.INCUMBENT_REFUSED,
                EnumTypedDecisionReason.NO_REPOSITORY_ATTRIBUTION,
                detail="the request names no work-unit repository",
            )
        if self._visibility_cache.is_public(request.work_unit_repository):
            return None
        # A token only widens what the read can SEE, never what counts as
        # public: the verdict below is the ``private`` field of a 200, so an
        # authenticated 200 for a private repository is still a refusal.
        url = f"{GITHUB_REST_URL}/repos/{request.work_unit_repository}"
        headers = {"Accept": "application/vnd.github+json"}
        token = self._visibility_token()
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._scoping.visibility_timeout_ms / 1000.0,
            ) as client:
                response = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            return self._visibility_unresolved(request, type(exc).__name__)
        if response.status_code == 404:
            return self._incumbent(
                request,
                EnumTypedDecisionDecider.INCUMBENT_REFUSED,
                EnumTypedDecisionReason.REPOSITORY_NOT_PUBLIC,
                detail=f"{request.work_unit_repository} is private or does not exist",
            )
        if response.status_code != 200:
            return self._visibility_unresolved(
                request, f"visibility read returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError:
            return self._visibility_unresolved(request, "visibility read was not JSON")
        if not isinstance(payload, dict) or payload.get("private") is not False:
            return self._incumbent(
                request,
                EnumTypedDecisionDecider.INCUMBENT_REFUSED,
                EnumTypedDecisionReason.REPOSITORY_NOT_PUBLIC,
                detail=f"{request.work_unit_repository} is not reported public",
            )
        self._visibility_cache.record_public(request.work_unit_repository)
        return None

    def _visibility_token(self) -> str | None:
        """The GitHub token for the visibility read, or None to read anonymously.

        Optional by contract: no token, or a store that cannot answer, means an
        anonymous read, which is the same guard at the lower rate cap.
        """
        if not self._github_token_resolved:
            self._github_token_resolved = True
            try:
                secret = resolve_api_key_loop_safe(
                    contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN"),
                    store=self._secret_store,
                    required=False,
                )
            except SecretResolutionError:
                secret = None
            self._github_token = secret.get_secret_value() if secret else None
        return self._github_token

    def _visibility_unresolved(
        self, request: ModelTypedDecisionRequest, detail: str
    ) -> ModelTypedDecisionResult:
        return self._incumbent(
            request,
            EnumTypedDecisionDecider.INCUMBENT_REFUSED,
            EnumTypedDecisionReason.REPOSITORY_VISIBILITY_UNRESOLVED,
            detail=detail,
        )

    # ------------------------------------------------------------------ mapping

    @staticmethod
    def _request_body(
        request: ModelTypedDecisionRequest, backend: ModelResolvedDelegationBackend
    ) -> dict[str, Any]:
        question: dict[str, Any] = {
            "type": request.kind.value,
            "instructions": request.instructions,
        }
        if request.criteria is not None:
            question["criteria"] = request.criteria
        return {
            "state": request.state,
            "model": backend.model_id,
            "questions": {_QUESTION_ID: question},
        }

    def _from_answer(
        self,
        request: ModelTypedDecisionRequest,
        backend: ModelResolvedDelegationBackend,
        response: httpx.Response,
        latency_ms: int,
    ) -> ModelTypedDecisionResult:
        receipt: dict[str, Any] = {
            "backend": backend,
            "http_status": response.status_code,
            "latency_ms": latency_ms,
        }
        try:
            payload = response.json()
            answer = payload["answers"][_QUESTION_ID]
            if answer.get("type") != request.kind.value:
                raise ValueError(f"answer type {answer.get('type')!r}")
            parsed = self._parse_answer(request, answer)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            return self._incumbent(
                request,
                EnumTypedDecisionDecider.INCUMBENT_BACKEND_ERROR,
                EnumTypedDecisionReason.BACKEND_MALFORMED_RESPONSE,
                detail=f"{type(exc).__name__}: {exc}"[:_DETAIL_LIMIT],
                **receipt,
            )
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        served_model = payload.get("model")
        model_answer, probability, probabilities, confidence, score = parsed
        threshold = self._routing.abstain_below_probability
        abstained = probability < threshold
        decided_by = EnumTypedDecisionDecider.MODEL
        if abstained:
            decided_by = (
                EnumTypedDecisionDecider.NO_ANSWER
                if request.incumbent_answer is None
                else EnumTypedDecisionDecider.INCUMBENT_ABSTAINED
            )
        return ModelTypedDecisionResult(
            correlation_id=request.correlation_id,
            decided_by=decided_by,
            answer=request.incumbent_answer if abstained else model_answer,
            reason=(
                EnumTypedDecisionReason.BELOW_ABSTENTION_THRESHOLD
                if abstained
                else None
            ),
            model_answer=model_answer,
            probability=probability,
            confidence=confidence,
            probabilities=probabilities,
            score=score,
            abstain_below_probability=threshold,
            backend_id=backend.backend_id,
            requested_model=backend.model_id,
            served_model=served_model if isinstance(served_model, str) else None,
            http_status=response.status_code,
            latency_ms=latency_ms,
            input_tokens=_int_or_none(usage.get("input_tokens")),
            output_tokens=_int_or_none(usage.get("output_tokens")),
        )

    @staticmethod
    def _parse_answer(
        request: ModelTypedDecisionRequest, answer: dict[str, Any]
    ) -> tuple[str, float, dict[str, float], float | None, float | None]:
        """Return (model_answer, probability, probabilities, confidence, score)."""
        confidence = answer.get("confidence")
        confidence_value = float(confidence) if confidence is not None else None
        if request.kind is EnumTypedDecisionKind.NOUL:
            p_true = float(answer["noul"])
            if not 0.0 <= p_true <= 1.0:
                raise ValueError(f"noul {p_true} outside [0, 1]")
            model_answer = "true" if p_true >= 0.5 else "false"
            return (
                model_answer,
                max(p_true, 1.0 - p_true),
                {"true": p_true, "false": 1.0 - p_true},
                confidence_value,
                None,
            )
        probabilities = {
            str(k): float(v) for k, v in dict(answer["probabilities"]).items()
        }
        if not probabilities:
            raise ValueError("no probabilities")
        if request.kind is EnumTypedDecisionKind.CHOICE:
            model_answer = str(answer["choice"])
            if (
                not isinstance(request.criteria, dict)
                or model_answer not in request.criteria
            ):
                raise ValueError(f"choice {model_answer!r} is not an offered option")
            return (
                model_answer,
                probabilities[model_answer],
                probabilities,
                confidence_value,
                None,
            )
        # score: the answer is the most probable level; the weighted position
        # is kept alongside it.
        model_answer = max(probabilities, key=lambda level: probabilities[level])
        levels = len(request.criteria) if isinstance(request.criteria, list) else 0
        if model_answer not in {str(i) for i in range(levels)}:
            raise ValueError(f"score level {model_answer!r} is not an offered level")
        return (
            model_answer,
            probabilities[model_answer],
            probabilities,
            confidence_value,
            float(answer["score"]),
        )

    def _incumbent(
        self,
        request: ModelTypedDecisionRequest,
        decided_by: EnumTypedDecisionDecider,
        reason: EnumTypedDecisionReason,
        *,
        backend: ModelResolvedDelegationBackend | None = None,
        http_status: int | None = None,
        latency_ms: int | None = None,
        detail: str | None = None,
    ) -> ModelTypedDecisionResult:
        return ModelTypedDecisionResult(
            correlation_id=request.correlation_id,
            decided_by=(
                EnumTypedDecisionDecider.NO_ANSWER
                if request.incumbent_answer is None
                else decided_by
            ),
            answer=request.incumbent_answer,
            reason=reason,
            abstain_below_probability=self._routing.abstain_below_probability,
            backend_id=backend.backend_id if backend else self._routing.backend_id,
            requested_model=backend.model_id if backend else None,
            http_status=http_status,
            latency_ms=latency_ms,
            detail=detail[:_DETAIL_LIMIT] if detail else None,
        )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__: list[str] = ["HandlerTypedDecision"]
