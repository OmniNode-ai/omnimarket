# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build and deliver provider quota observations (OMN-20154).

Every metered provider call produces one :class:`ModelProviderQuotaObserved`.
Three call paths make metered calls, and each delivers the same event:

* the runtime delegation orchestrator returns it as a published event (the
  runtime publishes it on the contract topic; orchestrators emit, they do not
  publish);
* the in-process delegation effect (``onex delegate`` without a broker) and
  the judge hand it to a :class:`ProtocolProviderQuotaObservationSink`, whose
  default delivers through ``node_event_emit_effect`` (spool outbox, then the
  bus), so an observation made on a laptop reaches the same projection.

"Metered" means the quota policy declares the endpoint's provider. A call to
an undeclared host (a local model) has no quota and emits nothing.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from omnimarket.events.provider_quota import (
    EnumProviderQuotaOutcome,
    EnumProviderQuotaSource,
    ModelProviderQuotaObserved,
    credential_ref_for,
)
from omnimarket.inference.provider_quota_policy import (
    ModelQuotaVerdict,
    classify_quota_response,
)
from omnimarket.inference.provider_quota_state import (
    endpoint_is_metered,
    quota_domain_for_endpoint,
    resolve_quota_tenant,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumQuotaDisposition,
)

_logger = logging.getLogger(__name__)

#: The topic the projection subscribes. Declared by the projection node's
#: contract and by the orchestrator's; the sink reads it from there.
_OBSERVED_TOPIC_SUFFIX = "provider-quota-observed.v1"  # onex-topic-allow: suffix used only for contract lookup

# ``HandlerInferenceIntent._provider_http_error_message`` shape:
#   "provider HTTP 429 Too Many Requests for <url>; retry_after=17; response_body={...}"
_HTTP_STATUS_PATTERN = re.compile(r"provider HTTP (\d{3})\b")
_RETRY_AFTER_PATTERN = re.compile(r";\s*retry_after=([^;]+?);")
_BODY_PATTERN = re.compile(r"response_body=(.*)\Z", re.DOTALL)
# ``HandlerLlmDelegationCall`` shape: "Client error '429 Too Many Requests' ..."
_HTTPX_STATUS_PATTERN = re.compile(r"(?:Client|Server) error '(\d{3})\b")


class ModelParsedProviderError:
    """HTTP status, provider body and Retry-After recovered from an error text."""

    __slots__ = ("body", "headers", "http_status")

    def __init__(
        self,
        http_status: int | None,
        body: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> None:
        self.http_status = http_status
        self.body = body
        self.headers = headers


_CODE_FIELD_PATTERN = re.compile(r'"code"\s*:\s*"?([0-9A-Za-z_]+)"?')
_STATUS_FIELD_PATTERN = re.compile(r'"status"\s*:\s*"([A-Z_]+)"')


def _parse_provider_body(raw: str) -> dict[str, Any] | None:
    """The provider's error body, tolerant of the shapes that reach us.

    Google wraps its error in a one-element JSON array, and the runtime bounds
    the body it carries, so a long Gemini body arrives truncated and is no
    longer JSON. When a whole parse fails, the three fields classification
    needs (``code``, ``status`` and the message text) are read out of the raw
    text instead, and nothing else is inferred.
    """
    if not raw or raw[0] not in "[{":
        return None
    try:
        parsed: Any = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        parsed = parsed[0]
    if isinstance(parsed, dict):
        return parsed
    code = _CODE_FIELD_PATTERN.search(raw)
    status = _STATUS_FIELD_PATTERN.search(raw)
    if code is None and status is None:
        return None
    error: dict[str, Any] = {"message": raw}
    if code is not None:
        error["code"] = code.group(1)
    if status is not None:
        error["status"] = status.group(1)
    return {"error": error}


def parse_provider_error_message(error_message: str) -> ModelParsedProviderError:
    """Recover the status, body and Retry-After a provider failure carried.

    The runtime inference response carries only ``error_message``, so this is
    the one place its structure is read back. Nothing here guesses: a status
    that is not stated is ``None`` and a body that does not parse as JSON is
    ``None``.
    """
    status: int | None = None
    match = _HTTP_STATUS_PATTERN.search(error_message) or _HTTPX_STATUS_PATTERN.search(
        error_message
    )
    if match:
        status = int(match.group(1))
    body: dict[str, Any] | None = None
    body_match = _BODY_PATTERN.search(error_message)
    if body_match is None:
        marker = "provider response:"
        index = error_message.find(marker)
        raw = error_message[index + len(marker) :] if index >= 0 else ""
    else:
        raw = body_match.group(1)
    body = _parse_provider_body(raw.strip())
    headers: dict[str, str] = {}
    retry = _RETRY_AFTER_PATTERN.search(error_message)
    if retry:
        headers["Retry-After"] = retry.group(1).strip()
    return ModelParsedProviderError(status, body, headers)


def build_quota_observation(
    *,
    tenant_id: object,
    endpoint_url: str,
    api_key_ref: str | None,
    model_name: str,
    succeeded: bool,
    observed_at: datetime,
    latency_ms: int,
    source: EnumProviderQuotaSource,
    correlation_id: UUID | None = None,
    http_status: int | None = None,
    verdict: ModelQuotaVerdict | None = None,
    error_message: str = "",
) -> ModelProviderQuotaObserved | None:
    """One observation for one metered call, or ``None`` for an unmetered one.

    ``verdict`` is the classifier's answer for a 429; a verdict that is still
    ``retryable`` (an unmapped code) is observed as a failed call, never as a
    block, which is the classifier's own fail direction.
    """
    if not endpoint_is_metered(endpoint_url):
        return None
    provider_id = quota_domain_for_endpoint(endpoint_url)
    if provider_id is None or not model_name.strip():
        return None
    tenant = resolve_quota_tenant(tenant_id)
    started = observed_at - timedelta(milliseconds=max(latency_ms, 0))
    common: dict[str, Any] = {
        "event_id": uuid4(),
        "tenant_id": tenant,
        "credential_ref": credential_ref_for(api_key_ref),
        "provider_id": provider_id,
        "model_name": model_name.strip(),
        "http_status": http_status,
        "call_started_at": started,
        "observed_at": observed_at,
        "correlation_id": correlation_id,
        "source": source,
    }
    if succeeded:
        return ModelProviderQuotaObserved(
            outcome=EnumProviderQuotaOutcome.CALL_OK, **common
        )
    if verdict is not None and not verdict.retryable:
        return ModelProviderQuotaObserved(
            outcome=EnumProviderQuotaOutcome.LIMIT_HIT,
            provider_code=verdict.provider_code,
            disposition=verdict.disposition.value,
            block_scope=verdict.scope.value,
            blocked_until=verdict.disabled_until,
            blocked_indefinitely=(
                verdict.disposition is EnumQuotaDisposition.DISABLE_UNTIL_BILLING
            ),
            reason=verdict.reason[:2000],
            **common,
        )
    return ModelProviderQuotaObserved(
        outcome=EnumProviderQuotaOutcome.CALL_FAILED,
        provider_code=verdict.provider_code if verdict is not None else None,
        reason=(verdict.reason if verdict is not None else error_message)[:2000],
        **common,
    )


def observe_failed_call(
    *,
    tenant_id: object,
    endpoint_url: str,
    api_key_ref: str | None,
    model_name: str,
    error_message: str,
    observed_at: datetime,
    latency_ms: int,
    source: EnumProviderQuotaSource,
    correlation_id: UUID | None = None,
    http_status: int | None = None,
    body: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[ModelProviderQuotaObserved | None, ModelQuotaVerdict | None]:
    """Classify one failed call and build its observation.

    ``http_status``/``body``/``headers`` are used when the caller holds the
    real response; otherwise they are recovered from ``error_message``.
    """
    if http_status is None:
        parsed = parse_provider_error_message(error_message)
        http_status = parsed.http_status
        body = parsed.body if body is None else body
        headers = parsed.headers if headers is None else headers
    verdict: ModelQuotaVerdict | None = None
    if http_status == 429:
        try:
            verdict = classify_quota_response(
                status_code=429,
                endpoint_url=endpoint_url,
                body=body,
                headers=headers,
                now=observed_at,
            )
        except Exception as exc:  # never convert a failure into a crash
            _logger.warning("provider quota classification unavailable: %s", exc)
    observation = build_quota_observation(
        tenant_id=tenant_id,
        endpoint_url=endpoint_url,
        api_key_ref=api_key_ref,
        model_name=model_name,
        succeeded=False,
        observed_at=observed_at,
        latency_ms=latency_ms,
        source=source,
        correlation_id=correlation_id,
        http_status=http_status,
        verdict=verdict,
        error_message=error_message,
    )
    return observation, verdict


class ProtocolProviderQuotaObservationSink(Protocol):
    """Delivers observations from a path that has no orchestrator to return them."""

    def emit(self, observation: ModelProviderQuotaObserved) -> None: ...


def provider_quota_observed_topic() -> str:
    """The observed topic, read from the projection node's contract."""
    from pathlib import Path

    from omnimarket.nodes.contract_topics import contract_subscribe_topics

    contract = (
        Path(__file__).resolve().parents[1]
        / "nodes"
        / "node_projection_provider_quota"
        / "contract.yaml"
    )
    for topic in contract_subscribe_topics(contract):
        if topic.endswith(_OBSERVED_TOPIC_SUFFIX):
            return topic
    raise RuntimeError(
        f"{contract} declares no topic ending {_OBSERVED_TOPIC_SUFFIX!r}"
    )


class EmitEffectQuotaObservationSink:
    """Deliver through ``node_event_emit_effect``: spool first, then the bus.

    The spool makes delivery survive a missing broker; the effect publishes the
    backlog on its next invocation. Never raises: an observation that cannot be
    delivered is logged, and the provider re-asserts a refusal on the next call.
    """

    def emit(self, observation: ModelProviderQuotaObserved) -> None:
        from omnimarket.events.emit_effect_topic_publisher import (
            EmitEffectTopicPublisher,
        )

        try:
            topic = provider_quota_observed_topic()
        except Exception as exc:
            _logger.warning("provider quota observation topic unresolved: %s", exc)
            return
        EmitEffectTopicPublisher().publish(
            topic=topic,
            event_type="provider.quota.observed",
            payload=observation.model_dump(mode="json"),
            event_id=str(observation.event_id),
            correlation_id=(
                str(observation.correlation_id)
                if observation.correlation_id is not None
                else None
            ),
            partition_key=(
                f"{observation.tenant_id}:{observation.credential_ref}:"
                f"{observation.provider_id}"
            ),
        )


__all__ = [
    "EmitEffectQuotaObservationSink",
    "ModelParsedProviderError",
    "ProtocolProviderQuotaObservationSink",
    "build_quota_observation",
    "observe_failed_call",
    "parse_provider_error_message",
    "provider_quota_observed_topic",
]
