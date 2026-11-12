# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every model a customer's key can use, with the details a picker shows (OMN-23413).

:mod:`byok_model_discovery` reads a provider's list-models endpoint to pick one
preferred model. A model picker needs the whole list, with what the provider
publishes about each model: its display name, context window and price per
million tokens (OpenRouter publishes all three; Google publishes a display name
and token limits; OpenAI publishes ids only, so nothing is invented for it).

The read is the same one discovery makes: one GET to the catalogue row's
``models_url`` with the key in the one header the row's ``models_auth`` names.
The key is never logged, stored or returned; a provider's message is scrubbed of
credential shapes before it is returned.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.handlers.routing import byok_model_discovery as discovery
from omnimarket.handlers.routing.byok_provider_backends import (
    resolve_byok_provider_backend,
)
from omnimarket.inference.provider_response_error import (
    failure_class_for_status,
    provider_message_from_text,
)

_PER_MILLION = 1_000_000


class ModelByokListedModel(BaseModel):
    """One model on a key's list. A detail the provider does not publish is None."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    context_tokens: int | None = None
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None


class ModelByokModelListing(BaseModel):
    """A provider's model list for one key. Never carries the key.

    ``outcome``: ``listed`` (the list was read; it may still be empty),
    ``rejected`` (the key was refused), ``billing`` (refused on billing),
    ``unsupported`` (the provider has no list to read), ``inconclusive``
    (anything else: a timeout, a 5xx, an unreadable body).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    outcome: Literal["listed", "rejected", "billing", "unsupported", "inconclusive"]
    models: tuple[ModelByokListedModel, ...] = ()
    message: str | None = None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _per_million(value: Any) -> float | None:
    """A per-token price string as dollars per million; a negative price
    (OpenRouter's "varies") or an unreadable one is None, not zero."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number * _PER_MILLION, 6) if number >= 0 else None


def _entries(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    entries = body.get("data")
    if not isinstance(entries, list):
        entries = body.get("models")
    return (
        [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    )


def _listed(entry: dict[str, Any]) -> ModelByokListedModel | None:
    raw = entry.get("id") or entry.get("name")
    if not isinstance(raw, str) or not raw.strip():
        return None
    model_id = raw.strip().removeprefix("models/")
    pricing_raw = entry.get("pricing")
    pricing: dict[str, Any] = pricing_raw if isinstance(pricing_raw, dict) else {}
    top_raw = entry.get("top_provider")
    top: dict[str, Any] = top_raw if isinstance(top_raw, dict) else {}
    label = entry.get("displayName") or (entry.get("name") if "id" in entry else None)
    return ModelByokListedModel(
        id=model_id,
        name=str(label).strip() if label else model_id,
        context_tokens=_positive_int(
            entry.get("context_length")
            or top.get("context_length")
            or entry.get("inputTokenLimit")
        ),
        input_usd_per_million=_per_million(pricing.get("prompt")),
        output_usd_per_million=_per_million(pricing.get("completion")),
    )


def list_byok_models(
    provider: str,
    api_key: str | SecretStr,
    *,
    get: discovery.GetJson | None = None,
) -> ModelByokModelListing:
    """Read ``provider``'s list-models endpoint with ``api_key``; every model, sorted.

    ``get`` is looked up at CALL time when omitted, so the test suite's guard
    on :func:`byok_model_discovery.get_models_json` also covers this read.
    """
    backend = resolve_byok_provider_backend(provider)
    if backend is None or backend.endpoint_from_registration or not backend.models_url:
        return ModelByokModelListing(
            provider=provider,
            outcome="unsupported",
            message=f"{provider} has no model list to read; type the model id instead.",
        )
    secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
    if not secret.strip():
        return ModelByokModelListing(
            provider=provider, outcome="rejected", message="enter an API key first"
        )
    fetch = get if get is not None else discovery.get_models_json
    try:
        body = fetch(
            url=backend.models_url,
            timeout_seconds=discovery.DISCOVERY_TIMEOUT_SECONDS,
            extra_headers=discovery.models_request_headers(backend, secret.strip()),
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        text = exc.response.text if isinstance(exc.response.text, str) else ""
        failure = failure_class_for_status(status, text)
        outcome: Literal["rejected", "billing", "inconclusive"] = "inconclusive"
        if failure is EnumDelegationFailureClass.PROVIDER_AUTH_FAILED:
            outcome = "rejected"
        elif failure is EnumDelegationFailureClass.PROVIDER_BILLING:
            outcome = "billing"
        message = provider_message_from_text(text) or f"HTTP {status}"
        return ModelByokModelListing(
            provider=provider,
            outcome=outcome,
            message=message.replace(secret.strip(), "[key]"),
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        # The class name only: an exception's text can echo the request.
        return ModelByokModelListing(
            provider=provider,
            outcome="inconclusive",
            message=f"could not read the model list ({type(exc).__name__}); try again",
        )
    unique: dict[str, ModelByokListedModel] = {}
    for entry in _entries(body):
        listed = _listed(entry)
        if listed is not None:
            unique.setdefault(listed.id, listed)
    return ModelByokModelListing(
        provider=provider,
        outcome="listed",
        models=tuple(sorted(unique.values(), key=lambda m: m.id.lower())),
        message=None if unique else "the provider listed no models for this key",
    )
