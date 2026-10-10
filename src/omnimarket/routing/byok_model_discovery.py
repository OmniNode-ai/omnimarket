# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which model a customer's key can actually use, read from the provider (OMN-20157).

Why this exists
---------------
The BYOK catalogue used to pin one model id per provider. A provider retires
ids for NEW accounts while old accounts keep them: on 2026-09-30 a fresh Google
AI Studio key got HTTP 404 "no longer available to new users" for the pinned
``gemini-2.5-flash-lite``, while the lab's older project still answered on it.
A pin tested on our own key therefore says nothing about a customer's key.

So the catalogue declares a model PREFERENCE per provider row
(``model_preference``) and the provider's own list-models endpoint
(``models_url``), and this module asks the provider, with the customer's key,
which ids that key may use, then picks the best match
(:func:`omnimarket.routing.byok_provider_backends.select_byok_model`).

Where it runs
-------------
* At registration: ``onex secret set``, the hosted intake and plan detection.
  The resolved model is stored with the credential.
* At call time, in the effect that holds the key: when a route's model is the
  unresolved marker, before the call; and when the provider answers 404
  model-not-found, once, excluding the model that failed. A second failure is a
  typed ``PROVIDER_MODEL_NOT_FOUND`` refusal, never a loop.

What it never does
------------------
It never logs, stores or returns the key. The key travels only as the bearer
header of one GET to the catalogue's ``models_url``, posted verbatim. Results
carry statuses, ids and the provider's own message with credential shapes
scrubbed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.inference.provider_response_error import (
    failure_class_for_status,
    provider_message_from_text,
)
from omnimarket.routing.byok_provider_backends import (
    ModelByokProviderBackend,
    select_byok_model,
)

logger = logging.getLogger(__name__)

#: Bound on one list-models read. Registration waits on it, and so does a
#: call-time re-resolve, so it is short; a slow provider reads as inconclusive.
DISCOVERY_TIMEOUT_SECONDS = 15.0

#: Google names its models ``models/<id>``; the chat endpoint takes ``<id>``.
_RESOURCE_PREFIX = "models/"

#: The list-models read's call shape: ``get(url=, timeout_seconds=,
#: extra_headers=)`` returns the decoded JSON body or raises
#: ``httpx.HTTPStatusError`` with the provider's status and body. Tests pass a
#: fake; production uses :func:`get_models_json`.
GetJson = Callable[..., Any]


def get_models_json(
    *,
    url: str,
    timeout_seconds: float,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    """GET a provider's list-models endpoint VERBATIM and return its JSON body.

    Through the delegation effect's contract transport
    (:func:`get_provider_json`), the same module every provider call of this
    package goes through. A non-2xx raises ``httpx.HTTPStatusError`` with the
    provider's status and body preserved, so one classifier
    (``failure_class_for_status``) reads the list and the chat call alike.
    """
    # Imported here, not at module level: the delegation effect's package
    # imports this module, so a top-level import would be circular.
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
        get_provider_json,
    )

    return get_provider_json(
        url=url, timeout_seconds=timeout_seconds, extra_headers=extra_headers
    )


class ModelByokModelDiscovery(BaseModel):
    """What a provider's model list said about one key. Never carries the key.

    ``outcome``:

    * ``resolved``: the list was read and ``model`` is the best preferred id on it.
    * ``no_match``: the list was read and names none of the preferred families.
    * ``not_listed``: the customer named a model (OMN-20844) and the list, read
      for their key, does not name it.
    * ``rejected``: the provider refused the key itself (401/403, or Google's 400
      "API key not valid").
    * ``billing``: the provider refused on the account's billing.
    * ``inconclusive``: anything else (a timeout, a 5xx, a throttle, an
      unreadable body). Not evidence about the key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    plan: str
    outcome: Literal[
        "resolved", "no_match", "not_listed", "rejected", "billing", "inconclusive"
    ]
    model: str | None = None
    listed_count: int = 0
    http_status: int | None = None
    provider_message: str | None = None
    #: OMN-20844: the model the customer named, when the list was read to check it.
    chosen: str | None = None


def _model_ids(body: Any) -> tuple[str, ...]:
    """Every model id in an OpenAI-shaped (``data[].id``) or Google-shaped
    (``models[].name``) list, with Google's ``models/`` prefix removed."""
    if not isinstance(body, dict):
        return ()
    entries = body.get("data")
    key = "id"
    if not isinstance(entries, list):
        entries = body.get("models")
        key = "name"
    if not isinstance(entries, list):
        return ()
    ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = entry.get(key) or entry.get("id") or entry.get("name")
        if not isinstance(raw, str) or not raw.strip():
            continue
        model = raw.strip()
        if model.startswith(_RESOURCE_PREFIX):
            model = model[len(_RESOURCE_PREFIX) :]
        ids.append(model)
    return tuple(ids)


def discover_byok_model_sync(
    backend: ModelByokProviderBackend,
    api_key: str | SecretStr,
    *,
    exclude: Iterable[str] = (),
    exclude_families_of: Iterable[str] = (),
    chosen: str | None = None,
    get: GetJson | None = None,
) -> ModelByokModelDiscovery:
    """Read ``backend.models_url`` with ``api_key`` and pick the preferred model.

    Blocking; see :func:`discover_byok_model` for the async form. ``get`` is
    looked up at CALL time when omitted, so the test suite's autouse guard can
    replace :func:`get_models_json` and no unit test reaches a real provider.

    OMN-20844: ``chosen`` is the model the customer named. The list is then
    read only to check it: ``resolved`` with that model when the list names it,
    ``not_listed`` when it does not. The preference is never consulted, so no
    model the customer did not name is returned.
    """
    fetch = get if get is not None else get_models_json
    secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
    base = {"provider": backend.provider, "plan": backend.plan}
    try:
        response = fetch(
            url=backend.models_url,
            timeout_seconds=DISCOVERY_TIMEOUT_SECONDS,
            extra_headers={"Authorization": f"Bearer {secret}"},
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
        return ModelByokModelDiscovery(
            **base,
            outcome=outcome,
            http_status=status,
            provider_message=provider_message_from_text(text),
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        # The class name only: an exception's text can echo the request.
        logger.info(
            "byok model discovery for provider=%s plan=%s did not complete (%s)",
            backend.provider,
            backend.plan,
            type(exc).__name__,
        )
        return ModelByokModelDiscovery(**base, outcome="inconclusive")

    ids = _model_ids(response)
    if not ids:
        return ModelByokModelDiscovery(**base, outcome="inconclusive", http_status=200)
    if chosen is not None:
        return ModelByokModelDiscovery(
            **base,
            outcome="resolved" if chosen in ids else "not_listed",
            model=chosen if chosen in ids else None,
            listed_count=len(ids),
            http_status=200,
            chosen=chosen,
        )
    model = select_byok_model(
        backend, ids, exclude=exclude, exclude_families_of=exclude_families_of
    )
    return ModelByokModelDiscovery(
        **base,
        outcome="resolved" if model is not None else "no_match",
        model=model,
        listed_count=len(ids),
        http_status=200,
    )


async def discover_byok_model(
    backend: ModelByokProviderBackend,
    api_key: str | SecretStr,
    *,
    exclude: Iterable[str] = (),
    exclude_families_of: Iterable[str] = (),
    chosen: str | None = None,
    get: GetJson | None = None,
) -> ModelByokModelDiscovery:
    """Async form of :func:`discover_byok_model_sync` (runs it in a thread)."""
    return await asyncio.to_thread(
        discover_byok_model_sync,
        backend,
        api_key,
        exclude=tuple(exclude),
        exclude_families_of=tuple(exclude_families_of),
        chosen=chosen,
        get=get,
    )


#: OMN-20844: the typed code for a registration or a call on a row whose
#: customer chooses the model, when no model was named.
BYOK_MODEL_NOT_CHOSEN = "BYOK_MODEL_NOT_CHOSEN"


def model_not_chosen_message(provider: str) -> str:
    """What a customer is told when their key has no model they chose (OMN-20844)."""
    return (
        f"{BYOK_MODEL_NOT_CHOSEN}: you choose the {provider} model your key runs, "
        "and none was chosen. Name one with --model <model id> (for example "
        f"onex models add {provider} --model <model id>); the provider's model "
        "page lists the ids. A ':free' model is rate-limited but may be chosen."
    )


def describe_discovery_refusal(discovery: ModelByokModelDiscovery) -> str | None:
    """The customer-facing reason a registration cannot use this key, or ``None``.

    ``None`` for ``resolved`` and ``inconclusive``: the first needs no refusal,
    and the second is not evidence about the key (the model is resolved at call
    time instead).
    """
    said = (
        f' The provider said: "{discovery.provider_message}".'
        if discovery.provider_message
        else ""
    )
    status = f"HTTP {discovery.http_status}" if discovery.http_status else "no status"
    if discovery.outcome == "rejected":
        return (
            f"PROVIDER_AUTH_FAILED ({status}): {discovery.provider} refused that "
            f"key when asked which models it can use.{said} Copy the whole key "
            "from the provider's key page and try again."
        )
    if discovery.outcome == "billing":
        return (
            f"PROVIDER_BILLING ({status}): {discovery.provider} refused that key on "
            f"the account's billing.{said} The key is yours and so is the bill: "
            "add credits or enable billing on that provider account first."
        )
    if discovery.outcome == "not_listed":
        return (
            f"BYOK_MODEL_NOT_LISTED: {discovery.provider} does not list "
            f"{discovery.chosen!r} among the {discovery.listed_count} "
            "models your key can use. Check the model id on the provider's model "
            "page and try again."
        )
    if discovery.outcome == "no_match":
        return (
            f"BYOK_NO_PREFERRED_MODEL: {discovery.provider} lists "
            f"{discovery.listed_count} models for that key and none of them is "
            "one this catalogue uses for that provider, so no route can be made."
        )
    return None


__all__: list[str] = [
    "BYOK_MODEL_NOT_CHOSEN",
    "DISCOVERY_TIMEOUT_SECONDS",
    "ModelByokModelDiscovery",
    "describe_discovery_refusal",
    "discover_byok_model",
    "discover_byok_model_sync",
    "get_models_json",
    "model_not_chosen_message",
]
