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

**Native Messages protocol and prompt caching (OMN-20910).** The voters keep
this OpenAI-compatible surface; behind it each review is one native Messages
call (``api_native``, ``/v1/messages``) so the provider can cache the stable
prefix. The system message is split on ``BLOCK_BOUNDARY_MARKER`` into cached
system blocks (instructions and catalog, then the repository's standing rules)
and the user message into the diff (cached for five minutes, so the next persona
of the same review reads it) and the persona text (never cached), in that order.
A ``json_schema`` ``response_format`` becomes a forced tool and the tool's input
is returned as the message content. The provider's ``stop_reason`` rides back as
``finish_reason`` (``length`` for ``max_tokens``, never a silently shortened
body) and the cache usage rides back in ``usage``.

**A voter's reasoning-off request is honoured (OMN-19998).** The voter's
``chat_template_kwargs.enable_thinking`` false becomes the native ``thinking``
``{"type": "disabled"}`` for models that support it. A gateway whose host
configuration declares ``thinking_mode: adaptive`` instead sends adaptive
thinking and low effort: those models reject disabled thinking. A Claude model
that reasons by default counts its reasoning against ``max_tokens``; on a large
diff it spent the whole budget and answered nothing. The mode is a validated
deployment fact, never inferred from a model name.

Not here: a per-key budget ledger with a reserve, a sticky key per review run,
and the overlay entry.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnimarket.enums.enum_anthropic_stop_reason import EnumAnthropicStopReason
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_review_call_attempt_kind import EnumReviewCallAttemptKind
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.models.delegation.prompt_text_block import (
    ModelPromptMessage,
    ModelPromptTextBlock,
)
from omnimarket.models.review_cost import ModelReviewCallProvenance
from omnimarket.nodes.node_claude_review_gateway_effect.models.model_claude_review_gateway import (
    ModelClaudeReviewGatewayRequest,
    ModelReviewGatewayKey,
    ModelReviewGatewayPlan,
)
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend

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
#: The native thinking option for a voter's explicit reasoning-off request.
_DISABLED_THINKING = {"type": "disabled"}
#: The line the prompt builder puts between the parts of a message that are cached
#: apart: system instructions and catalog | standing rules, and diff | persona.
BLOCK_BOUNDARY_MARKER = "<<onex-cache-boundary>>"
#: The provider's backend plan this gateway names: Anthropic's native Messages API.
GATEWAY_PLAN = "api_native"
_SYSTEM_CACHE_TTL = "1h"
_DIFF_CACHE_TTL = "5m"
_DEFAULT_TOOL_NAME = "submit_verdict"
_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
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
    @property
    def stop_reason(self) -> EnumAnthropicStopReason | None: ...
    @property
    def cache_creation_input_tokens(self) -> int: ...
    @property
    def cache_read_input_tokens(self) -> int: ...


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


def _metadata_text(metadata: Mapping[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _metadata_count(metadata: Mapping[str, Any], key: str, default: int) -> int | None:
    """A non-negative integer from metadata (a digit string counts), else ``None``."""
    value = metadata.get(key, default)
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value if isinstance(value, int) and value >= 0 else None


def review_provenance(payload: Mapping[str, Any]) -> ModelReviewCallProvenance | None:
    """The review this call is made for, from metadata, or none (OMN-20910).

    All of the repository, run, pull request, head, voter and persona must be
    named; a call that names only part of it is served but not recorded as a
    review's, so a cost record never carries a guessed identity.
    """
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    texts = {
        key: _metadata_text(metadata, key)
        for key in ("repository", "review_run_id", "head_sha", "voter_id", "persona_id")
    }
    pr_number = _metadata_count(metadata, "pr", 0)
    chunk_index = _metadata_count(metadata, "chunk_index", 0)
    chunk_count = _metadata_count(metadata, "chunk_count", 1)
    if (
        any(value is None for value in texts.values())
        or not pr_number
        or chunk_index is None
        or not chunk_count
    ):
        return None
    try:
        return ModelReviewCallProvenance(
            review_run_id=str(texts["review_run_id"]),
            repository=str(texts["repository"]),
            pr_number=pr_number,
            head_sha=str(texts["head_sha"]),
            voter_id=str(texts["voter_id"]),
            persona_id=str(texts["persona_id"]),
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            attempt_kind=EnumReviewCallAttemptKind(
                _metadata_text(metadata, "attempt_kind") or "first"
            ),
            catalog_version=_metadata_text(metadata, "catalog_version") or "",
            prompt_version=_metadata_text(metadata, "prompt_version") or "",
        )
    except ValueError:
        return None


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


def workspace_header() -> str:
    """The header the catalogue declares for the gateway provider's workspace (OMN-20967)."""
    backend = resolve_byok_provider_backend(GATEWAY_PROVIDER)
    if backend is None or backend.workspace_header is None:
        raise RuntimeError(
            f"the BYOK catalogue declares no workspace header for {GATEWAY_PROVIDER}"
        )
    return backend.workspace_header


def _workspace_id(row: Mapping[str, object]) -> str | None:
    """The Anthropic workspace a catalogued key belongs to, or none (OMN-20967)."""
    value = row.get("workspace_id")
    return value.strip() or None if isinstance(value, str) else None


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
            workspace_id=_workspace_id(row),
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


def _parts(text: str) -> list[str]:
    """The non-empty parts of ``text`` between boundary markers."""
    return [part.strip() for part in text.split(BLOCK_BOUNDARY_MARKER) if part.strip()]


def _merge_tail(parts: list[str], keep: int) -> list[str]:
    """``parts`` as at most ``keep`` parts: the surplus joins the last kept one."""
    if len(parts) <= keep:
        return parts
    return [*parts[: keep - 1], "\n\n".join(parts[keep - 1 :])]


def _forced_tool(
    response_format: object,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The forced tool a ``json_schema`` response format asks for, else none.

    The tool's input is the schema-valid reply, so the verdict can never come
    back as free text. ``json_object`` and an absent format have no schema to
    enforce and call no tool.
    """
    if not isinstance(response_format, Mapping):
        return None, None
    if response_format.get("type") != "json_schema":
        return None, None
    spec = response_format.get("json_schema")
    if not isinstance(spec, Mapping) or not isinstance(spec.get("schema"), Mapping):
        return None, None
    raw_name = spec.get("name")
    name = (
        raw_name
        if isinstance(raw_name, str) and _TOOL_NAME.match(raw_name)
        else _DEFAULT_TOOL_NAME
    )
    description = spec.get("description")
    tool = {
        "name": name,
        "description": description
        if isinstance(description, str) and description
        else "Submit the structured result.",
        "input_schema": dict(spec["schema"]),
    }
    return tool, {"type": "tool", "name": name}


def has_user_text(payload: Mapping[str, Any]) -> bool:
    """Whether the review carries any user message text to send."""
    messages = payload.get("messages")
    return any(
        isinstance(message, Mapping)
        and message.get("role") != "system"
        and _text(message.get("content")).strip()
        for message in (messages if isinstance(messages, list) else [])
    )


def translate_chat_request(
    payload: Mapping[str, Any],
    *,
    model: str,
    endpoint_url: str,
    secret_ref: str,
    timeout_seconds: float,
    workspace_id: str | None = None,
    thinking_mode: Literal["disabled", "adaptive"] = "disabled",
) -> ModelLlmDelegationCallRequest:
    """The voter's chat call as one native Messages call on this host's key.

    System messages become cached system blocks and the rest the user message,
    in order, split at ``BLOCK_BOUNDARY_MARKER`` (see the module docstring for
    the layout). A ``json_schema`` response format becomes a forced tool. The
    model is the gateway's own; the voter's sampling is dropped.
    A key that belongs to an Anthropic workspace names it in the call's
    ``anthropic-workspace-id`` header (OMN-20967): the provider answers the
    key only with it.
    Only bool False requests the smallest reasoning budget. The host's declared
    thinking mode determines whether that is native disabled thinking or adaptive
    thinking at low effort; models that require adaptive thinking reject disabled.
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
    system = _merge_tail(_parts("\n\n".join(system_parts)), 2)
    system_blocks = tuple(
        ModelPromptTextBlock(text=part, cache_ttl=_SYSTEM_CACHE_TTL) for part in system
    )
    # Only the part before the last boundary is cached: it is what the next
    # persona of the same review rereads. The last part is the persona's own.
    prompt = _merge_tail(_parts("\n\n".join(part for part in prompt_parts if part)), 2)
    user_blocks = tuple(
        ModelPromptTextBlock(
            text=part,
            cache_ttl=_DIFF_CACHE_TTL if index < len(prompt) - 1 else None,
        )
        for index, part in enumerate(prompt)
    )
    tool_schema, tool_choice = _forced_tool(payload.get("response_format"))
    raw_max = payload.get("max_tokens", payload.get("max_completion_tokens"))
    max_tokens = (
        raw_max
        if isinstance(raw_max, int) and not isinstance(raw_max, bool) and raw_max > 0
        else _DEFAULT_MAX_TOKENS
    )
    request_id = str(uuid.uuid4())
    chat_template_kwargs = payload.get("chat_template_kwargs")
    options: dict[str, Any] = {}
    reasoning_off = (
        isinstance(chat_template_kwargs, Mapping)
        and chat_template_kwargs.get("enable_thinking") is False
    )
    if thinking_mode == "adaptive":
        options["thinking"] = {"type": "adaptive"}
        if reasoning_off:
            options["output_config"] = {"effort": "low"}
    elif reasoning_off:
        options["thinking"] = _DISABLED_THINKING.copy()
    return ModelLlmDelegationCallRequest(
        request_id=request_id,
        correlation_id=request_id,
        causation_id=request_id,
        model_id=model,
        model_id_source="claude review gateway --model",
        endpoint_ref=endpoint_url,
        prompt_hash=hashlib.sha256(
            "\n\n".join([*system, *prompt]).encode("utf-8")
        ).hexdigest(),
        task_type="code_review",
        max_tokens=max_tokens,
        temperature=_CLAUDE_TEMPERATURE,
        timeout_seconds=timeout_seconds,
        model_tier="cloud",
        provider=GATEWAY_PROVIDER,
        secret_ref=secret_ref,
        extra_headers=(
            {workspace_header(): workspace_id} if workspace_id is not None else {}
        ),
        inference_protocol="anthropic_messages",
        provider_request_options=options,
        system_blocks=system_blocks,
        messages=(ModelPromptMessage(role="user", blocks=user_blocks),),
        tool_schema=tool_schema,
        tool_choice=tool_choice,
        review=review_provenance(payload),
    )


def describe_call(request: ModelLlmDelegationCallRequest) -> dict[str, Any]:
    """The parts of a call a dry run shows: no prompt text, no key value."""
    blocks = [
        *request.system_blocks,
        *(block for message in request.messages for block in message.blocks),
    ]
    return {
        "endpoint_ref": request.endpoint_ref,
        "inference_protocol": request.inference_protocol,
        "model_id": request.model_id,
        "provider": request.provider,
        "secret_ref": request.secret_ref,
        "workspace_id": request.extra_headers.get(workspace_header()),
        "task_type": request.task_type,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "thinking": request.provider_request_options.get("thinking"),
        "output_config": request.provider_request_options.get("output_config"),
        "system_block_chars": [len(block.text) for block in request.system_blocks],
        "prompt_block_chars": [
            len(block.text) for message in request.messages for block in message.blocks
        ],
        "cache_breakpoints": [
            block.cache_ttl for block in blocks if block.cache_ttl is not None
        ],
        "forced_tool": None
        if request.tool_choice is None
        else request.tool_choice.get("name"),
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
    # ``max_tokens`` is the output budget cutting the reply off: answered as
    # ``length`` so the voter's parser sees a cut reply and never a finished one.
    finish_reason = (
        "length"
        if outcome.stop_reason is EnumAnthropicStopReason.MAX_TOKENS
        else "stop"
    )
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(datetime.now(UTC).timestamp()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": outcome.content or ""},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": outcome.tokens_in,
            "completion_tokens": outcome.tokens_out,
            "total_tokens": outcome.tokens_in + outcome.tokens_out,
            "prompt_tokens_details": {"cached_tokens": outcome.cache_read_input_tokens},
            "cache_creation_input_tokens": outcome.cache_creation_input_tokens,
            "cache_read_input_tokens": outcome.cache_read_input_tokens,
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
            workspace_id=key.workspace_id,
            thinking_mode=self._request.thinking_mode,
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
        if not has_user_text(payload):
            return ModelReviewGatewayPlan(
                host=self._request.host,
                tenant_id=self._request.tenant_id,
                model=self._request.model,
                upstream_url=self._endpoint_url,
                candidate_key_refs=tuple(key.api_key_ref for key in keys),
                chosen_key_ref=None,
                chosen_key_name=None,
                translated_request={},
                would_answer=400,
                reason="the review has no user message",
            )
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
        if not has_user_text(payload):
            return _error(400, "invalid_request", "the review has no user message")
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
    "BLOCK_BOUNDARY_MARKER",
    "CHAT_ROUTE",
    "GATEWAY_PLAN",
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
    "has_user_text",
    "normalize_repository",
    "review_repository",
    "run_dry",
    "select_host_keys",
    "serve_chat_call",
    "signature_is_valid",
    "translate_chat_request",
]
