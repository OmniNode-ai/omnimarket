# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude review gateway: one Hostile Review voter endpoint per lab host (OMN-21014).

The review voters overlay names each voter by an OpenAI-compatible
``endpoint_url``, and CI signs every call with the shared-secret HMAC header
the LLM transport sends. CI never holds a provider key. This node is the voter
endpoint that holds none either: on each call it reads the tenant's catalogued
``anthropic`` keys bound to this host (``tenant_inference_credentials``,
``bound_host``, OMN-21012) and hands the review to the delegation call effect
(``node_llm_delegation_call_effect``, the one sanctioned model caller) with the
chosen key's reference. The effect resolves the value through the secret store
at its own boundary, so no key value passes through this module.

**One host, one account.** Operator ruling 2026-10-11T17:58:45Z binds each
Claude account's key to the machine the account lives on and spreads reviews
across those machines. A gateway spends only keys bound to its own host, so the
overlay places reviews by naming one gateway per host and listing the others
as its ``overflow_endpoint_urls``.

**One repository never goes to the cloud.** Operator ruling
2026-10-13T20:50:03Z keeps exactly one repository's code away from cloud models
and allows every other repository. The gateway starts with that refused list
from a private overlay; the repository is never named in this public
repository. A review whose ``metadata.repository`` is on the list or absent
answers 503 so the overflow list moves it to a lab model. Neither case reads
a key.

**Fill-first, never round-robin.** Keys are tried in name order; the next one
serves only when the one before cannot (the scope's terms reading: one
account's credits at a time, never spread to multiply throughput).

**503 is the overflow signal.** No key to spend, a provider rate limit,
timeout, an unavailable model or any other failure that is not about the key
answers 503 at once, with no retry across keys, so the voter's overflow list
moves the review to the next host and then to a local model. A key the
provider refuses or bills out (auth, billing, missing credential, or the
credit-balance message) is set aside for the life of the process and the next
key on the host serves the same request.

Not here: a per-key budget ledger with a reserve, prompt caching on the native
Messages API, and the overlay entry.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.nodes.node_claude_review_gateway_effect.models.model_claude_review_gateway import (
    ModelClaudeReviewGatewayRequest,
    ModelReviewGatewayKey,
    ModelReviewGatewayPlan,
)

logger = logging.getLogger(__name__)

#: The catalog table node_projection_tenant_credentials projects (OMN-16316).
TENANT_CREDENTIALS_TABLE = "tenant_inference_credentials"
#: The provider whose keys this gateway spends.
GATEWAY_PROVIDER = "anthropic"
#: The header and secret the LLM HTTP transport signs every voter call with.
SIGNATURE_HEADER = "x-omn-node-signature"
SHARED_SECRET_ENV = "LOCAL_LLM_SHARED_SECRET"
#: The voter surface's chat route (the OpenAI-compatible path voters call).
CHAT_ROUTE = "/v1/chat/completions"
MODELS_ROUTE = "/v1/models"
#: Claude 4.7 and later refuse a temperature below 1 (and any top_p) on the
#: compatible endpoint with HTTP 400, so the voter's sampling is not forwarded.
_CLAUDE_TEMPERATURE = 1.0
_DEFAULT_MAX_TOKENS = 4096
#: Failures that are about the key, not the request: set it aside and try the next.
_KEY_FAILURES = frozenset(
    {
        EnumDelegationFailureClass.PROVIDER_AUTH_FAILED,
        EnumDelegationFailureClass.PROVIDER_BILLING,
        EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING,
    }
)
_CREDIT_EXHAUSTED_MARKERS = (
    "credit balance is too low",
    "enforced_spend_limit_reached",
)


class ProtocolKeyCatalog(Protocol):
    """Reads catalog rows (``SqliteDatabaseAdapter`` and the projection readers)."""

    def query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]: ...


class ProtocolKeyValues(Protocol):
    """Says whether a key reference resolves (``ProtocolSecretStore``'s read).

    Used only to skip a catalogued key whose value is missing; the value itself
    is resolved again, and only, by the delegation call effect.
    """

    async def get_secret(self, key: str) -> str | None: ...


class ProtocolCallOutcome(Protocol):
    """The fields of the delegation call effect's result this gateway reads."""

    @property
    def success(self) -> bool: ...
    @property
    def content(self) -> str | None: ...
    @property
    def failure_class(self) -> EnumDelegationFailureClass | None: ...
    @property
    def error_message(self) -> str | None: ...
    @property
    def tokens_in(self) -> int: ...
    @property
    def tokens_out(self) -> int: ...


class ProtocolReviewCall(Protocol):
    """Runs one delegation call (the delegation call effect's handler, in production)."""

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ProtocolCallOutcome: ...


def normalize_repository(name: str) -> str:
    """One repository name, regardless of owner, case or hyphen spelling."""
    return name.strip().lower().rsplit("/", 1)[-1].replace("-", "_")


def review_repository(payload: Mapping[str, Any]) -> str | None:
    """The repository this review names in metadata, or none."""
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    repository = metadata.get("repository")
    if not isinstance(repository, str):
        return None
    return repository.strip() or None


def _not_expired(value: object, now: datetime) -> bool:
    if value is None:
        return True
    if isinstance(value, datetime):
        expires_at = value
    else:
        try:
            expires_at = datetime.fromisoformat(str(value))
        except ValueError:
            return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > now


def select_host_keys(
    rows: Sequence[Mapping[str, object]],
    *,
    tenant_id: str,
    host: str,
    now: datetime,
) -> list[ModelReviewGatewayKey]:
    """The keys this host may spend, in the order they are tried.

    Only the tenant's ``anthropic`` keys bound to ``host`` that are neither
    revoked nor past their expiry. An unbound key is not this host's: a
    gateway spends an account's credits only on the machine the account lives
    on. Ordered by name, then reference, so the first key is the one drained
    first, whatever order the catalog returns.
    """
    keys = [
        ModelReviewGatewayKey(
            api_key_ref=str(row["api_key_ref"]),
            name=str(row.get("name") or row["api_key_ref"]),
            bound_host=host,
        )
        for row in rows
        if row.get("api_key_ref")
        and row.get("tenant_id") == tenant_id
        and row.get("provider") == GATEWAY_PROVIDER
        and row.get("bound_host") == host
        and row.get("revoked_at") is None
        and _not_expired(row.get("expires_at"), now)
    ]
    return sorted(keys, key=lambda key: (key.name, key.api_key_ref))


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        )
    return ""


def translate_chat_request(
    payload: Mapping[str, Any],
    *,
    model: str,
    endpoint_url: str,
    secret_ref: str,
    timeout_seconds: float,
) -> ModelLlmDelegationCallRequest:
    """The voter's chat call as one delegation call on this host's key.

    System messages become the system prompt and the rest the prompt, in
    order. The model is the gateway's own; the voter's sampling is dropped.
    """
    messages = payload.get("messages")
    system_parts: list[str] = []
    prompt_parts: list[str] = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, Mapping):
            continue
        text = _text(message.get("content"))
        if message.get("role") == "system":
            system_parts.append(text)
        else:
            prompt_parts.append(text)
    prompt = "\n\n".join(part for part in prompt_parts if part)
    raw_max = payload.get("max_tokens", payload.get("max_completion_tokens"))
    max_tokens = (
        raw_max
        if isinstance(raw_max, int) and not isinstance(raw_max, bool) and raw_max > 0
        else _DEFAULT_MAX_TOKENS
    )
    request_id = str(uuid.uuid4())
    return ModelLlmDelegationCallRequest(
        request_id=request_id,
        correlation_id=request_id,
        causation_id=request_id,
        model_id=model,
        model_id_source="claude review gateway --model",
        endpoint_ref=endpoint_url,
        prompt=prompt,
        prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        system_prompt="\n\n".join(part for part in system_parts if part),
        task_type="code_review",
        max_tokens=max_tokens,
        temperature=_CLAUDE_TEMPERATURE,
        timeout_seconds=timeout_seconds,
        model_tier="cloud",
        provider=GATEWAY_PROVIDER,
        secret_ref=secret_ref,
    )


def describe_call(request: ModelLlmDelegationCallRequest) -> dict[str, Any]:
    """The parts of a call a dry run shows: no prompt text, no key value."""
    return {
        "endpoint_ref": request.endpoint_ref,
        "model_id": request.model_id,
        "provider": request.provider,
        "secret_ref": request.secret_ref,
        "task_type": request.task_type,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "system_prompt_chars": len(request.system_prompt),
        "prompt_chars": len(request.prompt),
    }


def signature_is_valid(
    payload: Mapping[str, Any], signature: str | None, secret: str
) -> bool:
    """The transport's signature: HMAC-SHA256 over canonical JSON (sorted, compact).

    The canonical form is the one the sender computes: the LLM HTTP transport
    signs ``json.dumps(payload, sort_keys=True, separators=(",", ":"))`` before it
    posts, so a verifier over the raw wire bytes would refuse every real call.
    The signature carries no timestamp or nonce, the transport's documented limit
    on a private LAN segment; replay is bounded by the host-bound key set, not by
    this check.
    """
    if not signature or not secret:
        return False
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    expected = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _error(status: int, kind: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"type": kind, "message": message}}


def _completion(model: str, outcome: ProtocolCallOutcome) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(datetime.now(UTC).timestamp()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": outcome.content or ""},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": outcome.tokens_in,
            "completion_tokens": outcome.tokens_out,
            "total_tokens": outcome.tokens_in + outcome.tokens_out,
        },
    }


def _is_key_failure(outcome: ProtocolCallOutcome) -> bool:
    if outcome.failure_class in _KEY_FAILURES:
        return True
    message = (outcome.error_message or "").lower()
    return any(marker in message for marker in _CREDIT_EXHAUSTED_MARKERS)


class ClaudeReviewGateway:
    """Chooses this host's key and runs one review through the call effect, or 503."""

    def __init__(
        self,
        request: ModelClaudeReviewGatewayRequest,
        *,
        catalog: ProtocolKeyCatalog,
        key_values: ProtocolKeyValues,
        call: ProtocolReviewCall,
        endpoint_url: str,
    ) -> None:
        self._request = request
        self._catalog = catalog
        self._key_values = key_values
        self._call = call
        self._endpoint_url = endpoint_url
        self._set_aside: set[str] = set()
        self._cloud_refused_repositories = frozenset(
            normalize_repository(name) for name in request.cloud_refused_repositories
        )

    @property
    def set_aside(self) -> frozenset[str]:
        """References refused or out of credit since this process started."""
        return frozenset(self._set_aside)

    def candidate_keys(
        self, now: datetime | None = None
    ) -> list[ModelReviewGatewayKey]:
        rows = self._catalog.query(
            TENANT_CREDENTIALS_TABLE,
            {"tenant_id": self._request.tenant_id, "provider": GATEWAY_PROVIDER},
        )
        keys = select_host_keys(
            rows,
            tenant_id=self._request.tenant_id,
            host=self._request.host,
            now=now if now is not None else datetime.now(UTC),
        )
        return [key for key in keys if key.api_key_ref not in self._set_aside]

    def _translate(
        self, payload: Mapping[str, Any], key: ModelReviewGatewayKey
    ) -> ModelLlmDelegationCallRequest:
        return translate_chat_request(
            payload,
            model=self._request.model,
            endpoint_url=self._endpoint_url,
            secret_ref=key.api_key_ref,
            timeout_seconds=self._request.upstream_timeout_seconds,
        )

    def _repository_refusal(self, payload: Mapping[str, Any]) -> tuple[str, str] | None:
        repository = review_repository(payload)
        if repository is None:
            return (
                "gateway_repository_unnamed",
                "the review names no repository in metadata.repository, so it cannot "
                "be cleared for a cloud model; overflow to the next voter",
            )
        if normalize_repository(repository) in self._cloud_refused_repositories:
            return (
                "gateway_repository_refused",
                "the repository is refused: its diffs never go to a cloud model; "
                "overflow to the next voter",
            )
        return None

    async def plan(self, payload: Mapping[str, Any]) -> ModelReviewGatewayPlan:
        """The dry run: the key that would serve and the call it would make."""
        refusal = self._repository_refusal(payload)
        if refusal is not None:
            return ModelReviewGatewayPlan(
                host=self._request.host,
                tenant_id=self._request.tenant_id,
                model=self._request.model,
                upstream_url=self._endpoint_url,
                candidate_key_refs=(),
                chosen_key_ref=None,
                chosen_key_name=None,
                translated_request={},
                would_answer=503,
                reason=refusal[1],
            )
        keys = self.candidate_keys()
        chosen: ModelReviewGatewayKey | None = None
        for key in keys:
            if await self._key_values.get_secret(key.api_key_ref):
                chosen = key
                break
        if chosen is None:
            reason = (
                f"no live {GATEWAY_PROVIDER} key of tenant {self._request.tenant_id} "
                f"is bound to host {self._request.host}"
                if not keys
                else f"none of the {len(keys)} keys bound to host "
                f"{self._request.host} resolves in the secret store"
            )
        else:
            reason = f"key {chosen.name} would serve"
        return ModelReviewGatewayPlan(
            host=self._request.host,
            tenant_id=self._request.tenant_id,
            model=self._request.model,
            upstream_url=self._endpoint_url,
            candidate_key_refs=tuple(key.api_key_ref for key in keys),
            chosen_key_ref=None if chosen is None else chosen.api_key_ref,
            chosen_key_name=None if chosen is None else chosen.name,
            translated_request={}
            if chosen is None
            else describe_call(self._translate(payload, chosen)),
            would_answer=503 if chosen is None else 200,
            reason=reason,
        )

    async def complete(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Serve one chat completion on this host's first key that can, else 503."""
        refusal = self._repository_refusal(payload)
        if refusal is not None:
            return _error(503, *refusal)
        keys = self.candidate_keys()
        if not keys:
            return _error(
                503,
                "gateway_no_key",
                f"no live {GATEWAY_PROVIDER} key is bound to host {self._request.host}",
            )
        for key in keys:
            if not await self._key_values.get_secret(key.api_key_ref):
                logger.warning(
                    "review gateway: key %s does not resolve; set aside", key.name
                )
                self._set_aside.add(key.api_key_ref)
                continue
            outcome = await asyncio.to_thread(self._call, self._translate(payload, key))
            if outcome.success:
                return 200, _completion(self._request.model, outcome)
            if _is_key_failure(outcome):
                logger.warning(
                    "review gateway: key %s refused or out of credit (%s); "
                    "set aside, trying the next key on host %s",
                    key.name,
                    outcome.failure_class.value if outcome.failure_class else "unknown",
                    self._request.host,
                )
                self._set_aside.add(key.api_key_ref)
                continue
            failure = (
                outcome.failure_class.value if outcome.failure_class else "unknown"
            )
            return _error(
                503,
                "gateway_upstream_failed",
                f"the review call failed ({failure}); overflow to the next voter",
            )
        return _error(
            503,
            "gateway_keys_set_aside",
            f"every key bound to host {self._request.host} was refused or is out of credit",
        )


async def serve_chat_call(
    gateway: ClaudeReviewGateway,
    *,
    body: bytes,
    signature: str | None,
    secret: str,
) -> tuple[int, dict[str, Any]]:
    """One voter call, exactly as the chat route serves it.

    An unset gateway secret refuses every call (503, fail closed); a body that
    is not a JSON object is a 400; a missing or wrong signature is a 401. All
    three answer before any key is read.
    """
    if not secret:
        return _error(
            503,
            "gateway_not_configured",
            f"{SHARED_SECRET_ENV} is not set on the gateway; refusing every call",
        )
    try:
        payload = json.loads(body)
    except ValueError:
        return _error(400, "invalid_request", "the body is not JSON")
    if not isinstance(payload, dict):
        return _error(400, "invalid_request", "the body is not a JSON object")
    if not signature_is_valid(payload, signature, secret):
        return _error(401, "gateway_signature", "missing or invalid signature")
    return await gateway.complete(payload)


def build_app(gateway: ClaudeReviewGateway, *, model: str) -> FastAPI:
    """The voter surface: the models route and the signed chat route."""
    app = FastAPI(title="claude-review-gateway")

    @app.get(MODELS_ROUTE)
    async def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": model, "object": "model"}]}

    @app.post(CHAT_ROUTE)
    async def chat_completions(request: Request) -> JSONResponse:
        status, reply = await serve_chat_call(
            gateway,
            body=await request.body(),
            signature=request.headers.get(SIGNATURE_HEADER),
            secret=os.environ.get(SHARED_SECRET_ENV, ""),
        )
        return JSONResponse(reply, status_code=status)

    return app


class HandlerClaudeReviewGateway:
    """EFFECT handler: plan one review (dry run) or serve the voter endpoint."""

    def __init__(
        self,
        *,
        catalog: ProtocolKeyCatalog,
        key_values: ProtocolKeyValues,
        call: ProtocolReviewCall,
        endpoint_url: str,
    ) -> None:
        self._catalog = catalog
        self._key_values = key_values
        self._call = call
        self._endpoint_url = endpoint_url

    def gateway(self, request: ModelClaudeReviewGatewayRequest) -> ClaudeReviewGateway:
        return ClaudeReviewGateway(
            request,
            catalog=self._catalog,
            key_values=self._key_values,
            call=self._call,
            endpoint_url=self._endpoint_url,
        )

    async def handle(
        self,
        request: ModelClaudeReviewGatewayRequest,
        *,
        sample_payload: Mapping[str, Any] | None = None,
    ) -> ModelReviewGatewayPlan | None:
        gateway = self.gateway(request)
        if request.dry_run:
            return await gateway.plan(sample_payload or {})
        import uvicorn

        config = uvicorn.Config(
            build_app(gateway, model=request.model),
            host=request.bind_host,
            port=request.bind_port,
            log_level="info",
        )
        await uvicorn.Server(config).serve()
        return None


def run_dry(
    handler: HandlerClaudeReviewGateway,
    request: ModelClaudeReviewGatewayRequest,
    payload: Mapping[str, Any],
) -> ModelReviewGatewayPlan:
    """Synchronous dry run for the CLI."""
    if not request.dry_run:
        raise ValueError("run_dry needs a request with dry_run set")
    plan = asyncio.run(handler.handle(request, sample_payload=payload))
    if plan is None:
        raise RuntimeError("a dry run returned no plan")
    return plan


__all__ = [
    "CHAT_ROUTE",
    "GATEWAY_PROVIDER",
    "MODELS_ROUTE",
    "SHARED_SECRET_ENV",
    "SIGNATURE_HEADER",
    "TENANT_CREDENTIALS_TABLE",
    "ClaudeReviewGateway",
    "HandlerClaudeReviewGateway",
    "ProtocolCallOutcome",
    "ProtocolKeyCatalog",
    "ProtocolKeyValues",
    "ProtocolReviewCall",
    "build_app",
    "describe_call",
    "normalize_repository",
    "review_repository",
    "run_dry",
    "select_host_keys",
    "serve_chat_call",
    "signature_is_valid",
    "translate_chat_request",
]
