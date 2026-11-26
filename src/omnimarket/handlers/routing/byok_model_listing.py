# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every model a customer's key can use, with the details a picker shows (OMN-23413).

:mod:`byok_model_discovery` reads a provider's list-models endpoint to pick one
preferred model. A model picker needs the whole list, with what the provider
publishes about each model: its display name, context window and price per
million tokens (OpenRouter publishes all three; Google publishes a display name,
token limits and whether it thinks; Anthropic a display name, limits and
capabilities; Reflection limits, input kinds and reasoning; Hopscotch all of it
in a ``uniblock`` block; OpenCode all of it, with a type and a free tier only
OpenCode itself may use; OpenAI publishes ids only, so nothing is invented for
it). Each shape is the provider's own docs, read 2026-11-26 (OMN-26688).

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

#: The most pages one list read follows. Google and Anthropic send up to 1000
#: models a page when asked, so this is far past any real list; a provider that
#: keeps saying there is more past it is answered as inconclusive, not trusted.
MAX_LIST_PAGES = 20


class ModelByokListedModel(BaseModel):
    """One model on a key's list. A detail the provider does not publish is None."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    context_tokens: int | None = None
    max_output_tokens: int | None = None
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None
    # What the provider's row says the model takes; empty when it says nothing.
    supported_parameters: tuple[str, ...] = ()
    input_modalities: tuple[str, ...] = ()


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
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return None
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


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def _whole(value: Any) -> int | None:
    """A whole number of tokens as JSON states one: a bool, a fraction, a text
    number, zero or below is not one."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


#: Hopscotch's capability words (features/model-capabilities) -> what the
#: provider's row says the model takes. Only the word "yes" counts: "no" and
#: "unknown" are non-empty strings, and "unknown is not no".
_SAID_PARAMETERS = {"tools": "tools", "json_mode": "response_format"}
_SAID_INPUTS = {"vision": "image", "audio": "audio"}


def _rate(block: dict[str, Any], field: str) -> float | None:
    """Hopscotch's rate, "thousandths of the currency unit per million tokens"
    in the entry's ``currency_code`` (api-reference/models/list-models), as
    dollars per million. Only a USD rate is dollars; a missing rate (a model
    priced per unit) is unknown, never free."""
    currency = block.get("currency_code")
    if not isinstance(currency, str) or currency.upper() != "USD":
        return None
    value = block.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return round(value / 1000, 6)


def _extension(entry: dict[str, Any]) -> dict[str, Any]:
    """Hopscotch's ``uniblock`` block on a listed model (OMN-26688), or nothing."""
    block = entry.get("uniblock")
    return block if isinstance(block, dict) else {}


def _said(block: dict[str, Any], words: dict[str, str]) -> tuple[str, ...]:
    raw = block.get("capabilities")
    stated: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return tuple(name for word, name in words.items() if stated.get(word) == "yes")


#: Anthropic's capabilities (platform.claude.com/docs/en/api/models-list),
#: each ``{"supported": bool}`` or null -> what the row says the model takes.
_ANTHROPIC_PARAMETERS = {
    "structured_outputs": "structured_outputs",
    "thinking": "reasoning",
}
_ANTHROPIC_INPUTS = {"image_input": "image", "pdf_input": "file"}


def _supported(entry: dict[str, Any], names: dict[str, str]) -> tuple[str, ...]:
    """Anthropic's capability objects: only ``supported`` exactly true counts."""
    raw = entry.get("capabilities")
    stated: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return tuple(
        name
        for key, name in names.items()
        if isinstance(stated.get(key), dict) and stated[key].get("supported") is True
    )


def _reasons(entry: dict[str, Any]) -> tuple[str, ...]:
    """Reflection states a ``reasoning`` object for a model that reasons; Google
    states ``thinking: true``. Neither is a yes in any other shape."""
    if isinstance(entry.get("reasoning"), dict) or entry.get("thinking") is True:
        return ("reasoning",)
    return ()


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _next_page(base: str, body: Any) -> str | None:
    """The next page of a list, as its provider continues one: Google's
    ``nextPageToken`` as ``pageToken``; Anthropic's ``has_more`` with
    ``last_id`` as ``after_id``. None when the answer says it is the last."""
    if not isinstance(body, dict):
        return None
    token = body.get("nextPageToken")
    if isinstance(token, str) and token:
        return str(httpx.URL(base).copy_set_param("pageToken", token))
    last = body.get("last_id")
    if body.get("has_more") is True and isinstance(last, str) and last:
        return str(httpx.URL(base).copy_set_param("after_id", last))
    return None


def _says_more(body: Any) -> bool:
    """A page that says more follow (``has_more`` true), whether or not it
    names where they continue."""
    return isinstance(body, dict) and body.get("has_more") is True


#: OpenCode's list (opencode.ai/docs, models endpoint, read 2026-11-26) marks each
#: entry with the API it recommends; no other provider's row carries this key.
_OPENCODE_MARK = "recommended_api"
#: OpenCode's capability objects, each ``{"supported": bool}``.
_OPENCODE_PARAMETERS = {"tools": "tools", "reasoning": "reasoning"}
#: OpenCode's input media types, as the words the other lists use.
_MEDIA_WORDS = {
    "text": "text",
    "image": "image",
    "audio": "audio",
    "application": "file",
}


def _opencode_price(entry: dict[str, Any], side: str) -> float | None:
    """``cost.text.<side>.price``: USD per 1M tokens (the docs' pricing). A
    price OpenCode does not state is unknown, never free."""
    cost = entry.get("cost")
    text = cost.get("text") if isinstance(cost, dict) else None
    block = text.get(side) if isinstance(text, dict) else None
    price = block.get("price") if isinstance(block, dict) else None
    if isinstance(price, bool) or not isinstance(price, int | float) or price < 0:
        return None
    return round(float(price), 6)


def _opencode_limit(entry: dict[str, Any], field: str) -> int | None:
    limit = entry.get("limit")
    return _whole(limit.get(field)) if isinstance(limit, dict) else None


def _opencode_inputs(entry: dict[str, Any]) -> tuple[str, ...]:
    """``modalities.input`` media types ("image/png") as words ("image")."""
    modalities = entry.get("modalities")
    raw = modalities.get("input") if isinstance(modalities, dict) else None
    words: list[str] = []
    for media in _strings(raw):
        word = _MEDIA_WORDS.get(media.split("/", 1)[0])
        if word is not None and word not in words:
            words.append(word)
    return tuple(words)


def _opencode_parameters(entry: dict[str, Any]) -> tuple[str, ...]:
    said = _supported(entry, _OPENCODE_PARAMETERS)
    capabilities = entry.get("capabilities")
    if isinstance(capabilities, dict) and capabilities.get("structured_output") is True:
        said += ("structured_outputs",)
    return said


def _opencode_usable(entry: dict[str, Any]) -> bool:
    """A paid chat model: "Free-tier chat models are only available from within
    OpenCode. Other harnesses receive HTTP 403 for those models"; embedding,
    image, video, realtime and decision models do not chat."""
    if entry.get("type") != "chat":
        return False
    return not (
        _opencode_price(entry, "input") == 0 and _opencode_price(entry, "output") == 0
    )


def _chats(entry: dict[str, Any]) -> bool:
    """Google names what a model does in ``supportedGenerationMethods``; one
    that names some, none of them generateContent, cannot chat. OpenCode states
    a type and a price (OMN-26688)."""
    if _OPENCODE_MARK in entry:
        return _opencode_usable(entry)
    methods = entry.get("supportedGenerationMethods")
    if isinstance(methods, list) and methods:
        return "generateContent" in methods
    return True


def _listed(entry: dict[str, Any]) -> ModelByokListedModel | None:
    if not _chats(entry):
        return None
    raw = entry.get("id") or entry.get("name")
    if not isinstance(raw, str) or not raw.strip():
        return None
    model_id = raw.strip().removeprefix("models/")
    pricing_raw = entry.get("pricing")
    pricing: dict[str, Any] = pricing_raw if isinstance(pricing_raw, dict) else {}
    top_raw = entry.get("top_provider")
    top: dict[str, Any] = top_raw if isinstance(top_raw, dict) else {}
    arch_raw = entry.get("architecture")
    arch: dict[str, Any] = arch_raw if isinstance(arch_raw, dict) else {}
    block = _extension(entry)
    if _OPENCODE_MARK in entry:
        return _opencode_listed(model_id, entry)
    label = (
        entry.get("displayName")
        or (entry.get("name") if "id" in entry else None)
        or _text(entry.get("display_name"))  # Anthropic
        or _text(block.get("display_name"))  # Hopscotch
    )
    return ModelByokListedModel(
        id=model_id,
        name=str(label).strip() if label else model_id,
        context_tokens=_positive_int(
            entry.get("context_length")
            or top.get("context_length")
            or entry.get("inputTokenLimit")
        )
        or _whole(entry.get("max_input_tokens"))  # Anthropic
        or _whole(block.get("context_window_tokens")),
        max_output_tokens=_positive_int(entry.get("max_output_tokens"))
        or _positive_int(entry.get("outputTokenLimit"))
        or _positive_int(top.get("max_completion_tokens"))
        or _whole(entry.get("max_tokens"))  # Anthropic
        or _whole(block.get("max_output_tokens")),
        input_usd_per_million=_per_million(pricing.get("prompt"))
        if "prompt" in pricing
        else _rate(block, "input_rate_thousandths"),
        output_usd_per_million=_per_million(pricing.get("completion"))
        if "completion" in pricing
        else _rate(block, "output_rate_thousandths"),
        supported_parameters=_strings(entry.get("supported_parameters"))
        or (
            _said(block, _SAID_PARAMETERS)
            + _supported(entry, _ANTHROPIC_PARAMETERS)
            + _reasons(entry)
        ),
        input_modalities=_strings(arch.get("input_modalities"))
        or _strings(entry.get("input_modalities"))  # Reflection
        or _said(block, _SAID_INPUTS) + _supported(entry, _ANTHROPIC_INPUTS),
    )


def _opencode_listed(model_id: str, entry: dict[str, Any]) -> ModelByokListedModel:
    """One OpenCode entry, every detail as its list states it (OMN-26688)."""
    return ModelByokListedModel(
        id=model_id,
        name=_text(entry.get("name")) or model_id,
        context_tokens=_opencode_limit(entry, "context"),
        max_output_tokens=_opencode_limit(entry, "output"),
        input_usd_per_million=_opencode_price(entry, "input"),
        output_usd_per_million=_opencode_price(entry, "output"),
        supported_parameters=_opencode_parameters(entry),
        input_modalities=_opencode_inputs(entry),
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
    headers = discovery.models_request_headers(backend, secret.strip())
    bodies: list[Any] = []
    url: str | None = backend.models_url
    read: set[str] = set()
    try:
        # Every page, as the provider continues its list (OMN-26688, D5-D6).
        while url is not None and url not in read:
            if len(read) == MAX_LIST_PAGES:
                return ModelByokModelListing(
                    provider=provider,
                    outcome="inconclusive",
                    message=(
                        f"{provider} kept listing past {MAX_LIST_PAGES} pages; "
                        "try again"
                    ),
                )
            read.add(url)
            body = fetch(
                url=url,
                timeout_seconds=discovery.DISCOVERY_TIMEOUT_SECONDS,
                extra_headers=headers,
            )
            bodies.append(body)
            url = _next_page(backend.models_url, body)
            if url is None and _says_more(body):
                # More was promised with no way to ask for it: a part of the
                # list is not the whole list (OMN-26688, D6).
                return ModelByokModelListing(
                    provider=provider,
                    outcome="inconclusive",
                    message=f"{provider} listed only part of its models; try again",
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
    for entry in (e for body in bodies for e in _entries(body)):
        listed = _listed(entry)
        if listed is not None:
            unique.setdefault(listed.id, listed)
    return ModelByokModelListing(
        provider=provider,
        outcome="listed",
        models=tuple(sorted(unique.values(), key=lambda m: m.id.lower())),
        message=None if unique else "the provider listed no models for this key",
    )
