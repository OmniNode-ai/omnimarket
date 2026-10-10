# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerInferenceIntent — executes ModelInferenceIntent from the delegation orchestrator.

Subscribes to onex.cmd.omnibase-infra.delegation-inference-request.v1.
Receives ModelInferenceIntent (base_url carries the COMPLETE endpoint URL
resolved by the orchestrator routing decision and is posted VERBATIM — OMN-12815),
executes the LLM HTTP call, and publishes ModelInferenceResponseData to
onex.evt.omnibase-infra.inference-response.v1 so the orchestrator's
DispatcherInferenceResponse can consume it.

This handler is the Kafka-native inference-intent consumer for the delegation
chain — the orchestrator publishes the intent, this node consumes it (OMN-12294).

OMN-13215: every delegation tier — including the ceiling — executes through this
single canonical HTTP inference path. The ceiling tier is swappable across
providers (gemini / glm / openrouter / claude) purely via the routing
contract/overlay (per-tier provider + endpoint + model + ``api_key_ref``); no
shelled-CLI tier remains. The former ``cli://`` subprocess backend was removed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from omnibase_core.models.delegation.wire import (
    EnumCredentialSource,
    ModelDelegationContractEvidence,
    ModelDelegationRawResponse,
    ModelInferenceIntent,
    ModelInferenceResponseData,
)

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.inference.protocol_config import apply_inference_protocol
from omnimarket.inference.provider_finish_reason import (
    TRUNCATED_RESPONSE_ERROR_MESSAGE,
    finish_reason_from_choice,
    is_truncated_by_output_budget,
)
from omnimarket.inference.provider_response_error import (
    IN_BODY_ERROR_MESSAGE_PREFIX,
    describe_provider_refusal,
    failure_class_for_status,
    provider_error_from_body,
)
from omnimarket.inference.secret_store_resolver import (
    SecretResolutionError,
    resolve_api_key,
)
from omnimarket.models.model_call_correlation import (
    SELF_HOSTED_CORRELATION_HEADER,
    SELF_HOSTED_CORRELATION_QUERY_PARAM,
)
from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_inference_call_budget import (
    INFERENCE_TIMEOUT_LOG_TOKEN,
    load_inference_call_budget,
)
from omnimarket.routing.byok_model_discovery import (
    describe_discovery_refusal,
    discover_byok_model_sync,
    model_not_chosen_message,
)
from omnimarket.routing.byok_provider_backends import (
    BYOK_MODEL_UNRESOLVED,
    ModelByokProviderBackend,
    catalogue_prefers_model,
    resolve_byok_backend_by_endpoint,
)
from omnimarket.tenant_credential_ref import is_tenant_credential_ref

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

# Topic is sourced from the node contract at import time — never hardcoded inline.
# The _get_inference_response_topic() call below fails fast if the contract drifts.
_INFERENCE_RESPONSE_TOPIC_SUFFIX = (
    "inference-response.v1"  # onex-topic-allow: suffix used only for contract lookup
)
_RESERVED_PROVIDER_REQUEST_KEYS = frozenset(
    {"model", "messages", "max_tokens", "temperature", "response_format"}
)
_MAX_PROVIDER_ERROR_BODY_CHARS = 1000
# OMN-13215: every delegation tier (including the ceiling) executes through the
# canonical HTTP inference path. Endpoint URLs are COMPLETE verbatim URLs resolved
# per-tier from the routing contract + overlay. Non-HTTP schemes (the deleted
# ``cli://`` shell-out tier) are a config-drift error and fail closed here — there
# is no subprocess fallback.
_SUPPORTED_URL_SCHEMES = ("http://", "https://")

# OMN-20299: a request a self-hosted model server answered either joins a
# ``delegation_events`` run or bypassed the delegation nodes, and the server's
# own log is the one record no caller can skip. The .201 vLLM access log keeps
# each request's path and query string, so the run's correlation id rides in the
# query string, and in a header for any log that records headers. Only a
# private or loopback address gets them; a third-party provider never does.

# OMN-18852: the ceiling ONE outbound provider call may occupy, read from this
# node's own contract at import time (the same fail-fast posture as the topic
# resolution below). This consumer is single-partition and single-member, so a
# rung's duration is a global resource: the 300 s the local backend asked for
# is the WHOLE of the dispatch port's wait and 125 % of the delegate-skill
# handler's execution budget, and holding it produced 600 s of dead slot in a
# 25-minute window on 2026-09-19.
_INFERENCE_CALL_BUDGET = load_inference_call_budget()
_INFERENCE_TIMEOUT_CEILING_SECONDS: Final[float] = float(
    _INFERENCE_CALL_BUDGET.max_inference_duration_seconds
)


CREDENTIAL_UNRESOLVED_ONEX_CODE: Final[
    Literal["ONEX_MARKET_EFFECT_CREDENTIAL_UNRESOLVED"]
] = "ONEX_MARKET_EFFECT_CREDENTIAL_UNRESOLVED"

# OMN-18201: the two expectations under which a credential is REQUIRED. NONE is
# a positive declaration that the backend takes none, and an absent expectation
# is a producer that predates the field -- neither is in this set, so neither
# changes behaviour.
_CREDENTIAL_REQUIRED_EXPECTATIONS: frozenset[EnumCredentialSource] = frozenset(
    {EnumCredentialSource.CUSTOMER_KEY, EnumCredentialSource.HOUSE}
)


class _DeadlineByteStream(httpx.SyncByteStream):
    """Enforce the call's absolute deadline while reading response chunks."""

    def __init__(self, stream: httpx.SyncByteStream, *, deadline: float) -> None:
        self._stream = stream
        self._deadline = deadline

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._stream:
            if time.monotonic() > self._deadline:
                raise httpx.ReadTimeout("Inference call exceeded its total deadline")
            yield chunk

    def close(self) -> None:
        self._stream.close()


def _deadline_response_hook(*, deadline: float) -> Callable[[httpx.Response], None]:
    def hook(response: httpx.Response) -> None:
        stream = response.stream
        if isinstance(stream, httpx.SyncByteStream):
            response.stream = _DeadlineByteStream(stream, deadline=deadline)

    return hook


class CredentialUnresolvedError(RuntimeError):
    """The route required a credential and this boundary resolved none.

    OMN-18201. Raised BEFORE the outbound request, which is the whole point.
    The header is attached only when a value resolved, and the request is posted
    either way, so a route whose credential never arrived reaches the provider
    unauthenticated and the vendor answers with a 401 that comes back as the
    delegation own failure. Nothing in that sequence is distinguishable from an
    auth-free backend working correctly, which is what makes the loss invisible.

    OMN-18196 ``_credential_source_for`` records that outcome honestly as
    ``NONE``, but it records it after the call has already gone out. This error
    is the refusal that stops the call, and the two compose: the raise leaves
    ``credential_source`` at the ``NONE`` the classifier already assigned, so
    the error response still carries the credential fact without a second
    vocabulary.

    The message names the reference, or names its absence, because that is the
    distinction a receipt cannot reconstruct: whether the route carried a
    reference that resolved to nothing, or carried none at all.

    ``error_code`` is read by ``omnibase_infra`` boundary-failure terminal via
    ``_first_onex_code`` when the exception object reaches the boundary intact,
    and leads the message for the engine-flattened path (the same contract
    ``CustomerKeyRefusedError`` uses, OMN-17930).
    """

    error_code: str

    def __init__(
        self,
        *,
        expected: EnumCredentialSource,
        api_key_ref: str | None,
        tenant_id: str | None,
    ) -> None:
        self.expected = expected
        self.api_key_ref = api_key_ref
        self.tenant_id = tenant_id
        self.error_code = CREDENTIAL_UNRESOLVED_ONEX_CODE
        held = (
            f"reference {api_key_ref!r} resolved to no usable value"
            if api_key_ref
            else "the intent carried no credential reference at all"
        )
        super().__init__(
            f"[{CREDENTIAL_UNRESOLVED_ONEX_CODE}] "
            f"delegation.credential.unresolved: the route declared "
            f"expected_credential_source={expected.value} for tenant "
            f"{tenant_id!r} but {held}. Refusing the provider call: an "
            "outbound request with no Authorization header would spend the "
            "route unauthenticated and report the vendor for a condition the "
            "platform can refuse locally."
        )


class ProviderRefusalError(RuntimeError):
    """A typed provider refusal about the account or the model (OMN-20157).

    Raised for a 402 or billing refusal (``PROVIDER_BILLING``) and a 404
    model-not-found (``PROVIDER_MODEL_NOT_FOUND``). The message leads with the
    class value, which the orchestrator's text classifier matches first, and
    quotes the provider with credential shapes scrubbed.
    """

    def __init__(self, failure_class: EnumDelegationFailureClass, message: str) -> None:
        self.failure_class = failure_class
        marker = failure_class.value.upper()
        super().__init__(
            message if marker.lower() in message.lower() else f"{marker}: {message}"
        )


class ProviderThrottledError(RuntimeError):
    """The provider answered HTTP 429, keeping its existing error text (OMN-20555)."""


class ModelListUnavailableError(RuntimeError):
    """The provider's model list could not be read to resolve a route's model.

    OMN-20157. Worded with "unavailable" so the orchestrator reads the retryable
    ``MODEL_UNAVAILABLE``: it is not evidence about the key or the account.
    """


class ModelAttributionMismatchError(RuntimeError):
    """The configured model is not one the endpoint serves (OMN-17098).

    Raised BEFORE the chat-completion POST, so no model generated anything. The
    message leads with the ``model_attribution_mismatch`` marker the
    orchestrator's text classifier reads (the same marker ``HandlerLlmDelegationCall``
    emits under OMN-16419), and ``handle`` returns the failure response without a
    ``model_used``: the configured id is exactly the value this guard found to be
    false, so stamping it would restate the false attribution on the response.
    """


class InferenceUsageError(RuntimeError):
    """Provider returned a usable usage block but no acceptable content.

    OMN-13408: ``finish_reason=length`` (truncation) and an empty message body
    both fail the inference, but the provider STILL returns a real OpenAI-shaped
    ``usage`` block reporting the prompt/completion tokens it metered (and, on a
    reasoning model like ``gemini-2.5-flash``, the thinking tokens that pushed the
    response past ``max_tokens``). Carry that served usage on the exception so the
    error-path ``ModelInferenceResponseData`` reports the real tokens consumed
    instead of defaulting them to 0 — the prior behaviour silently dropped the
    metered usage, so the canonical ``delegation-failed.v1`` terminal recorded
    0/0/0 tokens and $0 cost even though a multi-second metered cloud call ran.

    A transport failure / non-2xx response (raised before any ``response.json()``)
    carries no usage and remains a plain exception with zero-token fallback.
    """

    def __init__(
        self,
        message: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        super().__init__(message)
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


def _parse_usage(data: dict[str, Any]) -> tuple[int, int, int]:
    """Parse the OpenAI-compatible ``usage`` block into (prompt, completion, total).

    Tolerant of a missing/blank usage block (zeros). ``total_tokens`` falls back
    to ``prompt + completion`` when the provider omits it; for reasoning models
    the provider-reported total can exceed ``prompt + completion`` (thinking
    tokens) and is preserved as reported.
    """
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0) or (
        prompt_tokens + completion_tokens
    )
    return prompt_tokens, completion_tokens, total_tokens


def _tenant_round_trip_fields(intent: ModelInferenceIntent) -> dict[str, Any]:
    """Return the tenant round-trip kwarg for the inference response, if supported.

    OMN-14280 (OMN-14208 slice-2 A-now): the inference effect READS the wire
    tenant the orchestrator stamped onto the intent and ACTS on it — it
    tenant-tags its logs (below) and echoes the tenant back onto
    ``ModelInferenceResponseData`` so the owning tenant is auditable on the
    response and the orchestrator can observability-cross-check it. Guarded on
    the response model exposing ``tenant_id`` (mirrors the producer guard) so
    the effect degrades cleanly against a pre-0.46.8 core during the coordinated
    release window instead of raising ``extra="forbid"`` on every call.
    """
    if "tenant_id" in getattr(ModelInferenceResponseData, "model_fields", {}):
        return {"tenant_id": getattr(intent, "tenant_id", None)}
    return {}


def _attempt_round_trip_fields(intent: ModelInferenceIntent) -> dict[str, Any]:
    """Return the inference-attempt round-trip kwarg for the response (OMN-15542).

    The effect is the only party that can prove which attempt a response came
    from: it holds the intent while the call is outstanding. Echoing the intent's
    ``inference_attempt_id`` back onto ``ModelInferenceResponseData`` is what lets
    the orchestrator reject a delayed response from a route it has already left,
    instead of stamping the previous attempt's model identity onto the current
    endpoint and tier.

    Applied on BOTH the success and the error path — an error response is what
    drives escalation, and a late error from a superseded attempt would otherwise
    escalate the ladder a second time off a route that already moved on.

    Emitted only when the intent actually carries an id and the response model
    exposes the field (mirrors the tenant guard above), so the effect degrades
    to pre-OMN-15542 behavior against an older core instead of raising on
    ``extra="forbid"``.
    """
    attempt_id = getattr(intent, "inference_attempt_id", None)
    if attempt_id is None:
        return {}
    if "inference_attempt_id" in getattr(
        ModelInferenceResponseData, "model_fields", {}
    ):
        return {"inference_attempt_id": attempt_id}
    return {}


def _get_inference_response_topic() -> str:
    """Return the full inference-response publish topic from the contract.

    Fails fast at import time if the contract no longer declares the topic,
    preventing silent mis-wiring.
    """
    declared = contract_publish_topics(_CONTRACT_PATH)
    for topic in declared:
        if topic.endswith(_INFERENCE_RESPONSE_TOPIC_SUFFIX):
            return topic
    raise RuntimeError(
        f"Contract {_CONTRACT_PATH} does not declare a publish topic ending with "
        f"{_INFERENCE_RESPONSE_TOPIC_SUFFIX!r}. "
        "Update the contract before using HandlerInferenceIntent."
    )


TOPIC_INFERENCE_RESPONSE: str = _get_inference_response_topic()


def _merge_request_options(
    base: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_request_options(existing, value)
        else:
            merged[key] = value
    return merged


def _build_messages_and_request_options(
    intent: ModelInferenceIntent,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    system_prompt, prompt, configured_request_options = apply_inference_protocol(
        system_prompt=intent.system_prompt,
        prompt=intent.prompt,
        model=intent.model,
    )
    intent_request_options = getattr(intent, "provider_request_options", None) or {}
    provider_request_options = _merge_request_options(
        configured_request_options,
        intent_request_options,
    )
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages, provider_request_options


def _response_contract_evidence_from_sent_payload(
    intent: ModelInferenceIntent,
    payload: dict[str, Any],
) -> ModelDelegationContractEvidence | None:
    """Record only the response-contract instruction the adapter actually sent."""
    instruction = getattr(intent, "response_contract_instruction", None)
    contract_sha256 = getattr(intent, "response_contract_sha256", None)
    output_shape = getattr(intent, "response_contract_output_shape", None)
    if instruction is None:
        return None
    if not isinstance(instruction, str) or not isinstance(contract_sha256, str):
        raise ValueError("response contract intent declaration is incomplete")
    messages = payload.get("messages")
    content = ""
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        first_message_content = messages[0].get("content")
        if isinstance(first_message_content, str):
            content = first_message_content
    return ModelDelegationContractEvidence(
        conveyed=instruction in content,
        validated=False,
        output_shape=output_shape,
        contract_sha256=contract_sha256,
        channel="messages[0].content",
    )


def _credential_source_for(
    api_key_ref: str | None,
    resolved_api_key: str | None,
) -> EnumCredentialSource:
    """Classify the credential this boundary actually resolved (OMN-18196).

    Axiom 9 forbids a customer route binding a house credential, and nothing
    durable recorded which one answered a given run, so the prohibition was
    unfalsifiable after the fact. This is the first-hand fact. It is derived
    from the resolution that selected the credential for THIS call, never from
    the model, the route, the tier name, or a tenant's configuration read
    afterwards -- the same model id is reachable on a customer's own key and on
    a house credential alike, which is precisely why a model name cannot stand
    in for this.

    The resolved VALUE is the discriminator, not the reference alone. A tenant
    whose registered credential was withdrawn keeps its routing overlay row
    with a blanked ``secret_ref`` (OMN-18191), so the binding still names the
    customer while carrying no reference and resolving to nothing. That call
    ran unauthenticated and is ``NONE``. Classifying on the ref alone would
    report it as ``CUSTOMER_KEY`` -- a receipt asserting a customer key
    answered a call no customer key touched.

    A value that is blank once stripped is not a credential, and is ``NONE``
    for the same reason. The resolver's emptiness test is a bare truthiness
    check that does not strip, so a stored value of spaces or tabs is treated
    as present and arrives here intact; the header built from it is ``Bearer``
    followed by nothing. Reading it as ``CUSTOMER_KEY`` would be the very
    misclassification the paragraph above says this discriminator exists to
    prevent, reached by a different route (found by the OMN-18201 lane).

    This function classifies; it does not sanitise. The value is never
    trimmed before use, so what the boundary sends is unchanged and a merely
    padded credential still reaches the provider exactly as stored. Refusing
    the call outright is a separate concern at a separate layer (OMN-18201);
    this only keeps the RECORD honest about what was resolved, which has to
    hold even for a call that refusal does not cover.
    """
    if resolved_api_key is None or not resolved_api_key.strip():
        return EnumCredentialSource.NONE
    if is_tenant_credential_ref(api_key_ref):
        return EnumCredentialSource.CUSTOMER_KEY
    return EnumCredentialSource.HOUSE


def _provenance_stamp_fields(
    intent: ModelInferenceIntent,
    credential_source: EnumCredentialSource | None,
) -> dict[str, Any]:
    """Return the route/provider/credential-source kwargs for the response.

    OMN-18079 added ``route``/``provider`` to the intent and the response and
    OMN-18172 added the provenance model, but no producer ever populated the
    pair: read live on 2026-09-11, no call site in this repo passed ``route=``
    or ``provider=`` at any stage, so every terminal carried ``None`` for both
    and the receipt clause that grades them could never be met. This helper is
    where they start carrying a value.

    ``route`` and ``provider`` are echoed from the intent: the routing
    authority declared them and the effect posted that endpoint verbatim, so
    echoing is a report of what was called, not a re-derivation.
    ``credential_source`` is NOT echoed -- it is this boundary's own finding,
    passed in by the caller from the resolution it performed.

    ``credential_source`` is ``None`` only when resolution itself never
    completed, and it is then omitted rather than guessed. A confirmed
    missing or empty binding reports ``NONE``, including when that finding
    refused the call before any provider request.

    Guarded on the response model exposing each field, mirroring
    ``_tenant_round_trip_fields``, so the effect degrades cleanly against a
    core that predates them instead of raising ``extra="forbid"`` on every
    call during a coordinated release window.
    """
    model_fields = getattr(ModelInferenceResponseData, "model_fields", {})
    stamped: dict[str, Any] = {}
    route = getattr(intent, "route", None)
    provider = getattr(intent, "provider", None)
    # route/provider are a validated pair on the response: stamp both or
    # neither, or the model raises on a half-populated provenance.
    if route and provider and "route" in model_fields and "provider" in model_fields:
        stamped["route"] = route
        stamped["provider"] = provider
    if credential_source is not None and "credential_source" in model_fields:
        stamped["credential_source"] = credential_source
    return stamped


def _resolve_api_key(api_key_ref: str | None) -> str | None:
    """Resolve an API-key reference at the provider-call effect boundary.

    Resolves the secret VALUE through the canonical ``ProtocolSecretStore``
    (OMN-12824) rather than reading ``os.environ`` directly. Fail-closed: a
    declared reference with no secret-store value raises. ``None`` reference →
    ``None`` (unauthenticated backend).
    """
    resolved = resolve_api_key(api_key_ref)
    if resolved is None:
        return None
    value = resolved.get_secret_value()
    # OMN-18201: a value that is blank once stripped is not a credential, and it
    # must not become an Authorization header. The store-side fail-closed check
    # is ``if not value``, which does NOT strip, so a stored value of spaces or
    # tabs is truthy there and arrives here intact. Building a header from it
    # sends ``Bearer`` followed by nothing -- a request the vendor answers by
    # reporting a missing Authentication header, which reads downstream as a
    # platform bug with no local trace of the real cause. Returning ``None``
    # routes it into the same refusal an absent value takes, and lets
    # ``_credential_source_for`` classify it ``NONE`` rather than as the
    # customer key the reference names.
    #
    # The value itself is never stripped before use. Trimming a registered
    # credential would silently alter it; the only judgement made here is
    # whether anything remains at all.
    if not value.strip():
        return None
    return value


def _merge_provider_request_options(
    payload: dict[str, Any],
    provider_request_options: dict[str, Any] | None,
) -> dict[str, Any]:
    if not provider_request_options:
        return payload
    reserved = _RESERVED_PROVIDER_REQUEST_KEYS.intersection(provider_request_options)
    if reserved:
        keys = ", ".join(sorted(reserved))
        raise ValueError(f"provider request options cannot override: {keys}")
    return {**payload, **provider_request_options}


def _provider_http_error_message(exc: httpx.HTTPStatusError) -> str:
    """Return a bounded provider error message with body context.

    Runtime logs previously collapsed provider 4xx responses to the generic
    httpx status text, which made Gemini/OpenAI-compatible failures impossible
    to diagnose from the typed inference-response event. The response body is
    provider-authored and should not contain request headers or API keys; keep it
    bounded anyway so the published failure stays small.
    """
    response = exc.response
    body = response.text.strip()
    if len(body) > _MAX_PROVIDER_ERROR_BODY_CHARS:
        body = body[:_MAX_PROVIDER_ERROR_BODY_CHARS] + "...[truncated]"
    if not body:
        body = "<empty>"
    # OMN-20154: the provider's Retry-After is the authority for how long a
    # capacity refusal lasts, and this message is the only thing the
    # orchestrator receives, so the header rides in it (before the body, whose
    # text is free-form).
    retry_after = (response.headers.get("retry-after") or "").strip()
    retry_part = f"retry_after={retry_after}; " if retry_after else ""
    return (
        f"provider HTTP {response.status_code} {response.reason_phrase} "
        f"for {response.request.url}; {retry_part}response_body={body}"
    )


def _customer_byok_row(intent: ModelInferenceIntent) -> ModelByokProviderBackend | None:
    """The BYOK catalogue row ``intent`` is a customer route on, or ``None``."""
    if not is_tenant_credential_ref(intent.api_key_ref):
        return None
    return resolve_byok_backend_by_endpoint(intent.base_url.strip())


def _re_aim_intent(
    intent: ModelInferenceIntent,
    byok: ModelByokProviderBackend,
    api_key: str,
    *,
    exclude: tuple[str, ...],
    exclude_families_of: tuple[str, ...] = (),
) -> ModelInferenceIntent:
    """``intent`` re-aimed at the best model the key's provider list offers.

    OMN-20157. Raises :class:`ProviderRefusalError` when no model can be
    resolved: the provider refused the key's billing, lists no other preferred
    model, or could not be read. A key the provider rejects outright raises the
    same class with ``PROVIDER_AUTH_FAILED``'s wording, which the orchestrator
    already classifies as an auth failure.
    """
    discovery = discover_byok_model_sync(
        byok, api_key, exclude=exclude, exclude_families_of=exclude_families_of
    )
    if discovery.model is not None:
        logger.info(
            "byok model resolved from the provider's list provider=%s plan=%s "
            "model=%s (was %s) correlation_id=%s",
            byok.provider,
            byok.plan,
            discovery.model,
            intent.model,
            intent.correlation_id,
        )
        return intent.model_copy(update={"model": discovery.model})
    reason = describe_discovery_refusal(discovery)
    if discovery.outcome == "billing":
        raise ProviderRefusalError(
            EnumDelegationFailureClass.PROVIDER_BILLING, reason or "provider_billing"
        )
    if discovery.outcome == "rejected":
        # Worded so the orchestrator's text classifier reads an auth failure.
        raise RuntimeError(reason or "provider_auth_failed: 401 unauthorized")
    if discovery.outcome == "no_match" and reason is not None:
        raise ProviderRefusalError(
            EnumDelegationFailureClass.PROVIDER_MODEL_NOT_FOUND, reason
        )
    raise ModelListUnavailableError(
        f"model list unavailable: could not read {byok.provider}'s model list at "
        f"{byok.models_url} to resolve this route's model; no request was sent"
    )


def _attempt_failure_class(exc: Exception) -> EnumDelegationFailureClass:
    """The class an attempt on the key's own list failed with (OMN-20555)."""
    if isinstance(exc, ProviderThrottledError):
        return EnumDelegationFailureClass.RATE_LIMITED
    if isinstance(exc, ProviderRefusalError):
        return exc.failure_class
    if _is_upstream_unavailable(exc):
        return EnumDelegationFailureClass.MODEL_UNAVAILABLE
    return EnumDelegationFailureClass.UNKNOWN


def _is_upstream_unavailable(exc: Exception) -> bool:
    """An aggregator's in-body upstream error in a 200 (OMN-18265, OMN-19205)."""
    return isinstance(exc, InferenceUsageError) and str(exc).startswith(
        IN_BODY_ERROR_MESSAGE_PREFIX
    )


def _is_self_hosted_endpoint(endpoint_url: str) -> bool:
    """Whether ``endpoint_url`` is a lab or local model server (OMN-20299)."""
    host = (urlparse(endpoint_url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def _resolve_effective_timeout(intent: ModelInferenceIntent) -> float:
    """Return the seconds this provider call may occupy, clamped to the ceiling.

    A ceiling, never a floor: a backend declaring 15 s still gets 15 s. What it
    refuses is a rung outliving the budget the caller is measured against, at
    the cost of the one global inference slot.
    """
    return max(1.0, min(_ceiling_seconds_for(intent), float(intent.timeout_seconds)))


def _ceiling_seconds_for(intent: ModelInferenceIntent) -> float:
    """The contract ceiling for the model this intent names (OMN-19432).

    The default is the effect contract's ``http_request`` ``timeout_seconds``; a
    model whose measured latency does not fit it has its own declared entry in
    ``model_timeout_seconds``. Read from the contract, keyed by the intent's
    model id, never from the request: the wire's ``timeout_seconds`` is
    producer-supplied and a bound that honours it bounds nothing.
    """
    return float(_INFERENCE_CALL_BUDGET.ceiling_for(intent.model))


def _inference_timeout_error(
    intent: ModelInferenceIntent,
    exc: httpx.TimeoutException,
    *,
    elapsed_seconds: float,
    resolved_timeout: float,
) -> RuntimeError:
    """Log the one greppable abandonment line and return the typed failure.

    Two defects are closed here, both measured rather than argued.

    First, the pre-existing generic ``except Exception`` logged
    ``error=str(exc)``, and ``str()`` of an httpx timeout raised through the
    transport is frequently the EMPTY STRING -- so the line read
    ``HandlerInferenceIntent failed: ... error=`` and the published
    ``ModelInferenceResponseData.error_message`` was ``""``. A timeout was
    therefore indistinguishable from any other failure in the log AND reported
    no error at all on the wire.

    Second, nothing carried the elapsed time or the timeout that elapsed, so a
    300 s dead slot and a 0.1 s malformed-payload failure printed the same
    shape. ``INFERENCE_TIMEOUT_LOG_TOKEN`` is a single token one grep over
    ``docker logs omninode-runtime-effects`` finds, and the line names the
    correlation, the model, the elapsed seconds, the timeout that was enforced
    and the timeout that was requested -- the last two differing is how a
    reader sees the clamp did its job.
    """
    logger.warning(
        "%s correlation_id=%s attempt_id=%s model=%s provider=%s "
        "elapsed_seconds=%.3f resolved_timeout_seconds=%.3f "
        "requested_timeout_seconds=%s ceiling_seconds=%.3f endpoint=%s "
        "exception=%s",
        INFERENCE_TIMEOUT_LOG_TOKEN,
        intent.correlation_id,
        getattr(intent, "inference_attempt_id", None),
        intent.model,
        intent.provider,
        elapsed_seconds,
        resolved_timeout,
        intent.timeout_seconds,
        _ceiling_seconds_for(intent),
        intent.base_url,
        type(exc).__name__,
    )
    # The transport's own words come FIRST and verbatim: they are the only part
    # of this message the provider authored, and an existing consumer may match
    # on them. Everything after is the boundary's own measurement, which is
    # what makes the message non-empty when httpx's is empty.
    detail = str(exc).strip() or type(exc).__name__
    return RuntimeError(
        f"{detail}: provider call timed out after {elapsed_seconds:.3f}s "
        f"against a resolved timeout of {resolved_timeout:.3f}s "
        f"(requested {intent.timeout_seconds}s, contract ceiling "
        f"{_ceiling_seconds_for(intent):.3f}s) for model "
        f"{intent.model} [{type(exc).__name__}]"
    )


class HandlerInferenceIntent:
    """Execute ModelInferenceIntent and return ModelInferenceResponseData.

    Receives the intent whose base_url carries the COMPLETE endpoint URL resolved
    by the routing reducer. Posts that URL VERBATIM (OMN-12815) and returns the
    response;
    the runtime dispatch-result applier publishes the returned model to
    TOPIC_INFERENCE_RESPONSE (the contract's publish_topics drives the
    auto-publish) — the handler does not publish directly.

    An LLM/transport failure is returned as a ModelInferenceResponseData with
    error_message set, so the failure is published and remains observable to the
    orchestrator (which escalates to the next tier).

    ``handle`` is the runtime dispatch entrypoint (handler_wiring resolves
    handle/handle_async, never __call__).
    """

    async def handle_async(
        self, intent: ModelInferenceIntent
    ) -> ModelInferenceResponseData:
        """Runtime dispatch entrypoint for async auto-wiring.

        ``handle`` remains the synchronous standalone/test entrypoint and uses
        the sync secret-store resolver. Runtime dispatch runs inside an active
        event loop, so invoking that sync path directly would make the resolver
        fail before the provider call. Run the sync effect in a worker thread so
        secret resolution and the blocking HTTP client stay off the runtime loop.
        """
        return await asyncio.to_thread(self.handle, intent)

    def handle(self, intent: ModelInferenceIntent) -> ModelInferenceResponseData:
        started = time.monotonic()
        call_id = str(uuid4())

        # OMN-18196: resolve the credential HERE, once, so the classification
        # and the value the call is made with come from the same resolution.
        # It stays inside the try: a declared reference with no stored value
        # fails closed in the resolver, and that failure must be returned as an
        # error response like any other so the orchestrator can escalate.
        # A confirmed missing binding reports NONE even when it refuses the
        # call. An unreadable store leaves the credential fact unknown.
        credential_source: EnumCredentialSource | None = None
        try:
            try:
                api_key = _resolve_api_key(intent.api_key_ref)
            except SecretResolutionError:
                # The resolver completed a store read and found no value.
                # Other failures leave the credential fact unknown.
                credential_source = EnumCredentialSource.NONE
                raise
            credential_source = _credential_source_for(intent.api_key_ref, api_key)
            # OMN-18201: fail closed BEFORE the request when the routing
            # authority said a credential was required and none resolved. The
            # header gate in ``_call_llm`` is a truthiness check, and on its own
            # it reads a missing credential as "this backend is
            # unauthenticated" -- correct for a local model on a customer
            # machine, and catastrophic for a cloud route whose reference went
            # missing. The two are indistinguishable here from the reference
            # alone, which is why the expectation travels on the intent. An
            # absent expectation keeps the pre-OMN-18201 behaviour, so this adds
            # a refusal and removes no existing capability. Raised inside the
            # same ``try`` as the resolution, so the refusal is returned as an
            # error response the orchestrator can act on, carrying the
            # ``credential_source`` the classifier above already assigned.
            expected = getattr(intent, "expected_credential_source", None)
            if not api_key and expected in _CREDENTIAL_REQUIRED_EXPECTATIONS:
                raise CredentialUnresolvedError(
                    expected=expected,
                    api_key_ref=intent.api_key_ref,
                    tenant_id=getattr(intent, "tenant_id", None),
                )
            return self._call_llm_on_resolved_model(
                intent,
                call_id,
                api_key=api_key,
                credential_source=credential_source,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            error_msg = str(exc)
            logger.warning(
                "HandlerInferenceIntent failed: model=%s correlation_id=%s "
                "tenant_id=%s error=%s",
                intent.model,
                intent.correlation_id,
                getattr(intent, "tenant_id", None),
                error_msg,
            )
            # OMN-13408: when the failure carries served usage (truncation /
            # empty-content errors where the provider still metered + reported
            # tokens), thread those real token counts onto the published
            # ModelInferenceResponseData so the orchestrator's terminal
            # delegation-failed.v1 (and its compat twin + the projection) record
            # the metered tokens and priced cost instead of defaulting to 0/0/0.
            # A transport failure carries no usage → zero fallback (unchanged).
            prompt_tokens = completion_tokens = total_tokens = 0
            if isinstance(exc, InferenceUsageError):
                prompt_tokens = exc.prompt_tokens
                completion_tokens = exc.completion_tokens
                total_tokens = exc.total_tokens
            return ModelInferenceResponseData(
                correlation_id=intent.correlation_id,
                content="",
                model_used=(
                    ""
                    if isinstance(exc, ModelAttributionMismatchError)
                    else intent.model
                ),
                llm_call_id=call_id,
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                error_message=error_msg,
                **_attempt_round_trip_fields(intent),
                **_tenant_round_trip_fields(intent),
                **_provenance_stamp_fields(intent, credential_source),
            )

    def _call_llm_on_resolved_model(
        self,
        intent: ModelInferenceIntent,
        call_id: str,
        *,
        api_key: str | None,
        credential_source: EnumCredentialSource | None,
    ) -> ModelInferenceResponseData:
        """Call the provider, resolving a customer route's model from its key.

        OMN-20157. On a customer's BYOK route (a tenant-shaped reference on an
        endpoint the BYOK catalogue declares) the model is the one the key
        resolved from the provider's own model list at registration. The
        unresolved marker is resolved here, before the call; a 404
        model-not-found is re-resolved ONCE from the same list excluding the
        failed model, and the call re-issued. OMN-20555 also re-aims a 429 or
        an aggregator's in-body upstream error ONCE, excluding the failed
        model's whole preference family. The backend and key stay the same.
        With no other listed family the first failure stands unchanged; a
        second failure names both attempts. Every house route stays one call.
        """
        byok = _customer_byok_row(intent)
        if byok is None or not api_key:
            return self._call_llm(
                intent, call_id, api_key=api_key, credential_source=credential_source
            )
        if byok.customer_chooses_model:
            # OMN-20844: the customer chooses this provider's model. A credential
            # with none is refused rather than given a catalogue pick, and a
            # model the customer named is called once and never swapped.
            if intent.model == BYOK_MODEL_UNRESOLVED:
                raise ProviderRefusalError(
                    EnumDelegationFailureClass.PROVIDER_MODEL_NOT_FOUND,
                    model_not_chosen_message(byok.provider),
                )
            if not catalogue_prefers_model(byok, intent.model):
                return self._call_llm(
                    intent,
                    call_id,
                    api_key=api_key,
                    credential_source=credential_source,
                )
        if intent.model == BYOK_MODEL_UNRESOLVED:
            intent = _re_aim_intent(intent, byok, api_key, exclude=())
        try:
            return self._call_llm(
                intent, call_id, api_key=api_key, credential_source=credential_source
            )
        except ProviderRefusalError as refusal:
            if (
                refusal.failure_class
                is not EnumDelegationFailureClass.PROVIDER_MODEL_NOT_FOUND
            ):
                raise
            try:
                retry = _re_aim_intent(intent, byok, api_key, exclude=(intent.model,))
            except (ProviderRefusalError, ModelListUnavailableError):
                # Nothing else to aim at: the provider's own 404 is the answer.
                raise refusal from None
            return self._call_llm(
                retry, call_id, api_key=api_key, credential_source=credential_source
            )
        except (ProviderThrottledError, InferenceUsageError) as first:
            # OMN-19205 on the bus path: the throttle belongs to the slug's
            # upstream, which its preference-family siblings share, so the one
            # switch leaves the whole family.
            if not isinstance(first, ProviderThrottledError) and not (
                _is_upstream_unavailable(first)
            ):
                raise
            try:
                retry = _re_aim_intent(
                    intent,
                    byok,
                    api_key,
                    exclude=(intent.model,),
                    exclude_families_of=(intent.model,),
                )
            except (ProviderRefusalError, ModelListUnavailableError):
                # Nothing else to aim at: the original provider failure stands.
                raise first from None
            try:
                return self._call_llm(
                    retry, call_id, api_key=api_key, credential_source=credential_source
                )
            except Exception as second:
                # Keep the exception's type and served usage; only the text
                # gains the attempt history.
                second.args = (
                    f"{second} | models tried on this key's own list: "
                    f"{intent.model} ({_attempt_failure_class(first).value}), "
                    f"{retry.model} ({_attempt_failure_class(second).value})",
                )
                raise

    def _call_llm(
        self,
        intent: ModelInferenceIntent,
        call_id: str,
        *,
        api_key: str | None,
        credential_source: EnumCredentialSource | None,
    ) -> ModelInferenceResponseData:
        # OMN-13215: every tier (including the ceiling) executes through this single
        # canonical HTTP inference path. The endpoint URL is the COMPLETE verbatim
        # URL resolved per-tier from the routing contract + overlay. A non-HTTP
        # scheme (e.g. the deleted ``cli://`` shell-out tier) is a config-drift
        # error and fails closed — there is no subprocess fallback.
        base_url = intent.base_url.strip()
        if not base_url.lower().startswith(_SUPPORTED_URL_SCHEMES):
            raise ValueError(
                "delegation inference requires a complete HTTP(S) endpoint URL "
                f"resolved from the routing contract/overlay; got {intent.base_url!r}. "
                "Non-HTTP backends (shelled-CLI tiers) are not supported — every "
                "tier including the ceiling must declare a complete chat-completions "
                "URL (OMN-13215)."
            )

        # OMN-17098: fail-closed model-attribution guard, the same one OMN-16419
        # put on ``HandlerLlmDelegationCall`` and the one this handler -- the path
        # the orchestrator actually dispatches to -- never had. The served-model
        # read runs BEFORE the POST, never after, because the response body's
        # echoed ``model`` is not evidence (SGLang echoes whatever was requested).
        # ``None`` is "no evidence either way" (most cloud backends expose no
        # ``/v1/models`` at this origin) and leaves behaviour unchanged.
        served_ids = transport.get_served_model_ids(base_url)
        if served_ids is not None and intent.model not in served_ids:
            raise ModelAttributionMismatchError(
                f"model_attribution_mismatch: configured model_name="
                f"{intent.model!r} is not in the served ids "
                f"{sorted(served_ids)!r} reported by "
                f"{transport.served_models_url(base_url)} (OMN-16419 fail-closed "
                "guard on the inference-intent path, OMN-17098 -- refusing to "
                "silently attribute this call to a model that is not running)."
            )

        messages, provider_request_options = _build_messages_and_request_options(intent)
        payload: dict[str, Any] = {
            "model": intent.model,
            "messages": messages,
            "max_tokens": intent.max_tokens,
            "temperature": intent.temperature,
        }
        if intent.response_format is not None:
            payload["response_format"] = intent.response_format
        payload = _merge_provider_request_options(
            payload,
            provider_request_options,
        )

        headers: dict[str, str] = {}
        # OMN-18196: the key was resolved once by ``handle`` and passed in, so
        # the value this header is built from and the credential class stamped
        # on the response come from the SAME resolution. Resolving again here
        # would allow the two to disagree, which is exactly the failure the
        # stamped field exists to make impossible.
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if intent.extra_headers:
            headers.update(intent.extra_headers)
        params: dict[str, str] | None = None
        if _is_self_hosted_endpoint(intent.base_url):
            correlation_id = str(intent.correlation_id)
            params = {SELF_HOSTED_CORRELATION_QUERY_PARAM: correlation_id}
            headers[SELF_HOSTED_CORRELATION_HEADER] = correlation_id

        timeout = _resolve_effective_timeout(intent)
        started = time.monotonic()

        # OMN-20299: the correlation query parameter is the client's, so the
        # POST below still takes ``intent.base_url`` verbatim (OMN-12815).
        # OMN-18852: httpx's per-phase timeout resets on each keep-alive chunk.
        # On 2026-10-03 OpenRouter's nvidia/nemotron-3-ultra-550b-a55b:free calls
        # took 359/502/600/958 s against the 120 s ceiling. Bound headers plus
        # body by the same absolute deadline so the next rung can run.
        deadline = started + timeout
        hook = _deadline_response_hook(deadline=deadline)
        with httpx.Client(
            timeout=timeout, params=params, event_hooks={"response": [hook]}
        ) as client:
            # OMN-12815: intent.base_url carries the COMPLETE endpoint URL
            # resolved by the routing authority; post it VERBATIM — no path
            # append, no construction.
            # The hook above fires only when a chunk arrives, so a silent
            # upstream could still hold the call for another per-phase
            # ``timeout`` past the deadline. Wait on the call for the time left
            # and abandon it there; leaving the ``with`` closes the client, and
            # the hook ends the abandoned worker at its next chunk.
            executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="inference-call"
            )
            try:
                response = executor.submit(
                    client.post,
                    intent.base_url,
                    json=payload,
                    headers=headers or None,
                    timeout=timeout,
                ).result(timeout=max(deadline - time.monotonic(), 0.0))
            except FutureTimeoutError as exc:
                raise _inference_timeout_error(
                    intent,
                    httpx.ReadTimeout("Inference call exceeded its total deadline"),
                    elapsed_seconds=time.monotonic() - started,
                    resolved_timeout=timeout,
                ) from exc
            except httpx.TimeoutException as exc:
                raise _inference_timeout_error(
                    intent,
                    exc,
                    elapsed_seconds=time.monotonic() - started,
                    resolved_timeout=timeout,
                ) from exc
            finally:
                executor.shutdown(wait=False)
            response_contract_evidence = _response_contract_evidence_from_sent_payload(
                intent, payload
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                refusal = failure_class_for_status(
                    exc.response.status_code, exc.response.text
                )
                if refusal is EnumDelegationFailureClass.RATE_LIMITED:
                    raise ProviderThrottledError(
                        _provider_http_error_message(exc)
                    ) from exc
                if refusal in (
                    EnumDelegationFailureClass.PROVIDER_BILLING,
                    EnumDelegationFailureClass.PROVIDER_MODEL_NOT_FOUND,
                ):
                    raise ProviderRefusalError(
                        refusal,
                        describe_provider_refusal(
                            refusal,
                            status_code=exc.response.status_code,
                            provider_text=exc.response.text,
                            model_id=intent.model,
                        ),
                    ) from exc
                raise RuntimeError(_provider_http_error_message(exc)) from exc
            data: dict[str, Any] = response.json()

        # OMN-13408: parse the provider's served ``usage`` block FIRST, before any
        # truncation / empty-content / empty-choices raise. The OpenAI-compatible
        # shim returns a real usage block even when ``finish_reason=length`` or the
        # message body is blank, so the failure paths below raise InferenceUsageError
        # carrying these metered counts — the error-path response then reports the
        # real tokens consumed instead of dropping them to 0.
        prompt_tokens, completion_tokens, total_tokens = _parse_usage(data)

        # OMN-18265: an aggregating provider does not always spend an HTTP
        # status on an upstream failure. OpenRouter answers 200 with no
        # ``choices`` and a top-level ``error`` object when the model's upstream
        # provider is what broke. Read that object BEFORE the empty-choices
        # branch below: "the provider is unavailable" and "the model answered
        # with nothing" are different facts, and the orchestrator's
        # non-retryable marker set carries the second one's exact wording, so
        # reporting the second for the first turns a transient outage into a
        # terminal refusal (live: correlation c1838c39, 2026-09-12T19:11:59Z).
        provider_error = provider_error_from_body(data)
        if provider_error is not None:
            raise InferenceUsageError(
                provider_error.as_error_message(),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

        choices = data.get("choices") or []
        if not choices:
            raise InferenceUsageError(
                "API returned empty choices array",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

        choice = choices[0]
        # OMN-18278: the comparison and the refusal message both live in
        # ``omnimarket.inference.provider_finish_reason`` now. This handler and
        # ``HandlerLlmDelegationCall`` are the two effect boundaries that read a
        # provider's ``choices[]``, and writing ``== "length"`` inline here is
        # exactly how the sibling came to have no truncation check at all.
        if is_truncated_by_output_budget(finish_reason_from_choice(choice)):
            raise InferenceUsageError(
                TRUNCATED_RESPONSE_ERROR_MESSAGE,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

        content_raw = choice.get("message", {}).get("content")
        if response_contract_evidence is not None and isinstance(content_raw, str):
            response_contract_evidence = response_contract_evidence.model_copy(
                update={
                    "raw_response": ModelDelegationRawResponse.from_provider_content(
                        content_raw, source_field="choices[0].message.content"
                    )
                }
            )
        content = content_raw.strip() if isinstance(content_raw, str) else ""
        if not content:
            raise InferenceUsageError(
                "API returned empty message content",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

        response_id: str = data.get("id") or call_id

        logger.info(
            "HandlerInferenceIntent succeeded: model=%s tokens=%d latency=%dms "
            "correlation_id=%s tenant_id=%s",
            intent.model,
            total_tokens,
            latency_ms,
            intent.correlation_id,
            getattr(intent, "tenant_id", None),
        )

        return ModelInferenceResponseData(
            correlation_id=intent.correlation_id,
            content=content,
            model_used=intent.model,
            llm_call_id=response_id,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            response_contract_evidence=response_contract_evidence,
            **_attempt_round_trip_fields(intent),
            **_tenant_round_trip_fields(intent),
            **_provenance_stamp_fields(intent, credential_source),
        )


__all__ = [
    "CREDENTIAL_UNRESOLVED_ONEX_CODE",
    "CredentialUnresolvedError",
    "HandlerInferenceIntent",
    "InferenceUsageError",
    "ModelAttributionMismatchError",
]
