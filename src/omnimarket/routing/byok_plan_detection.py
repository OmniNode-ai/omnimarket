# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Onboarding-time plan detection for a multi-plan BYOK provider (OMN-20157).

A provider can serve one key family through more than one product: z.ai's GLM
Coding Plan and its general pay-as-you-go API share a request shape and differ
in endpoint and metering. A key presented to the wrong product is refused there
(OMN-6790), so the route a customer's key gets has to be the product the key
belongs to. The customer should not have to know which; this module finds out.

How it decides
--------------
A provider's plans are split by the catalogue into the ones a customer may be
routed to (``customer_routable``) and the ones declared detection-only. z.ai's
subscription terms bar GLM Coding Plan quota from third-party systems, so the
Coding Plan is detection-only: it is tried only to RECOGNISE a key, never to
route one (knowledge-base-internal ``reference/zai-glm-coding-plan-terms.md``).

Each routable plan is tried first, the default plan first, by POSTing a
one-token completion to that plan's declared endpoint with the customer's key
and reading the answer:

* ``answered``: the key authenticated on that product.
* ``rejected``: the product refused the key (401/403, or z.ai 1113, the
  pay-as-you-go surface refusing a Coding-Plan key).
* ``inconclusive``: anything else (a throttle, a 5xx, a capacity code, a network
  failure). Not evidence about the key, so it is never read as a rejection.

Then:

* exactly one routable plan answered: ``detected``, that plan. A key a routable
  plan answers is usable there whatever else it can do, so the detection-only
  surfaces are NEVER contacted (a key that answers on both the general API and
  the Coding Plan is a general key).
* several routable plans answered: ``ambiguous``, no plan. The products meter
  differently, so choosing for the customer is a guess about whose money is
  spent. The caller asks the customer to name it.
* no routable plan answered and some were inconclusive: ``inconclusive``. A
  surface that could not be reached might have been the right one, so nothing
  is refused on this evidence.
* every routable plan rejected the key: each detection-only plan is now tried.
  One that answers means the key belongs only to a plan the provider's terms
  bar us from routing: ``not_permitted``, no plan, with ``refused_plan`` and the
  catalogue's typed ``refusal_code`` (z.ai: ``BYOK_CODING_PLAN_NOT_PERMITTED``).
  Otherwise ``rejected`` when every probe rejected the key, else
  ``inconclusive``.

Detection never guesses, and never returns a plan a customer may not be routed
to: ``plan`` is always a routable plan or ``None``.

An exhausted window (z.ai 1308, 1310, 1316, 1317) counts as ``answered``: the
key authenticated on that product and is capped, which is the plan.

What it does not do
-------------------
It never logs or returns the key, and the result carries only statuses and
provider codes. Provider-specific reading of the response is confined to the
small tables below; the quota mechanics that interpret the same codes at
delegation time are owned by OMN-20154.

The probe costs one list-models read and at most a one-token completion per
plan, each against the plan's preferred model on the key's own model list
(OMN-20157; the catalogue pins no model). A detection-only plan is probed only
for a key every routable plan refused.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
    ModelTransportResponse,
    post_chat_completion,
)
from omnimarket.routing.byok_model_discovery import (
    GetJson,
    discover_byok_model_sync,
)
from omnimarket.routing.byok_provider_backends import (
    ModelByokProviderBackend,
    byok_provider_plans,
    resolve_byok_declared_plan,
    resolve_byok_provider_backend,
)

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 20.0

#: The one-token probe budget. Named so the request is built from a declared
#: value and not a literal in a payload dict.
_PROBE_MAX_TOKENS = 1

#: The contract transport's call shape. It posts the catalogue's endpoint URL
#: verbatim and is the one HTTP path the delegation effect uses (OMN-13160), so
#: a probe reaches a provider exactly the way a delegation will.
PostChatCompletion = Callable[..., ModelTransportResponse]

#: HTTP statuses that mean "this product does not know this key".
_REJECTED_STATUSES = frozenset({401, 403})

#: z.ai codes that mean the product refused THIS key, on a 429. 1113 is the
#: pay-as-you-go surface answering a Coding-Plan key (OMN-6790).
_REJECTED_CODES = frozenset({"1000", "1001", "1002", "1003", "1004", "1113"})

#: z.ai codes on a 429 that mean the key authenticated and its window is spent.
_WINDOW_EXHAUSTED_CODES = frozenset({"1308", "1310", "1316", "1317"})


class ModelByokPlanProbe(BaseModel):
    """What one plan's endpoint said about the key. Never carries the key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan: str
    http_status: int | None
    provider_code: str | None
    verdict: Literal["answered", "rejected", "inconclusive"]
    #: OMN-20157. The model the probe asked for: the plan's preferred model on
    #: the key's own model list. ``None`` when the list could not be read, in
    #: which case no completion was sent.
    model: str | None = None


class ModelByokPlanDetection(BaseModel):
    """The result of detecting which plan a customer's key belongs to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    #: Always a plan a customer may be routed to, or ``None``.
    plan: str | None
    outcome: Literal[
        "detected",
        "ambiguous",
        "single_plan",
        "rejected",
        "inconclusive",
        "not_offered",
        "not_permitted",
    ]
    probes: tuple[ModelByokPlanProbe, ...] = ()
    #: ``not_permitted`` only: the detection-only plan the key answered on.
    refused_plan: str | None = None
    #: ``not_permitted`` only: the catalogue's typed refusal code for that plan.
    refusal_code: str | None = None
    #: OMN-20157. ``detected`` only: the model the key answered on for that plan,
    #: resolved from the key's own model list. Stored with the credential.
    model: str | None = None


def _error_code(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict) and error.get("code") is not None:
        return str(error["code"])
    if body.get("code") is not None:
        return str(body["code"])
    return None


def _classify(
    status: int | None, body: Any
) -> tuple[Literal["answered", "rejected", "inconclusive"], str | None]:
    code = _error_code(body)
    if status is None:
        return "inconclusive", None
    if status == 200:
        wraps_error = isinstance(body, dict) and body.get("error") is not None
        if not wraps_error and isinstance(body, dict) and body.get("choices"):
            return "answered", None
        # OMN-18265: an error carried inside a 200. Judge it by its own code.
        if code in _REJECTED_CODES or code in {"401", "403"}:
            return "rejected", code
        return "inconclusive", code
    if status in _REJECTED_STATUSES:
        return "rejected", code
    if code in _WINDOW_EXHAUSTED_CODES:
        return "answered", code
    if code in _REJECTED_CODES:
        return "rejected", code
    return "inconclusive", code


class _ModelProbeRequest(BaseModel):
    """The one-token completion a plan probe sends."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    messages: tuple[dict[str, str], ...]
    max_tokens: int = _PROBE_MAX_TOKENS
    stream: bool = False


def _probe_sync(
    post: PostChatCompletion,
    backend: ModelByokProviderBackend,
    api_key: str,
    get: GetJson | None = None,
) -> ModelByokPlanProbe:
    # OMN-20157: the probe model is the plan's preferred model on the key's OWN
    # model list, not a pinned id: a retired id answers 404 for a new key and
    # would read as "this plan does not know the key".
    discovery = discover_byok_model_sync(backend, api_key, get=get)
    if discovery.outcome == "rejected":
        return ModelByokPlanProbe(
            plan=backend.plan,
            http_status=discovery.http_status,
            provider_code=None,
            verdict="rejected",
        )
    if discovery.model is None:
        return ModelByokPlanProbe(
            plan=backend.plan,
            http_status=discovery.http_status,
            provider_code=None,
            verdict="inconclusive",
        )
    status: int | None = None
    body: Any = None
    request = _ModelProbeRequest(
        model=discovery.model,
        messages=({"role": "user", "content": "ping"},),
    )
    try:
        response = post(
            endpoint_url=backend.endpoint_url,
            payload=request.model_dump(mode="json"),
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
            extra_headers={"Authorization": f"Bearer {api_key}"},
        )
        status = response.status_code
        body = response.json_body
    except httpx.HTTPStatusError as exc:
        # The transport raises this for any non-2xx, on both of its transports,
        # with the provider's own status and body preserved.
        status = exc.response.status_code
        try:
            body = exc.response.json()
        except ValueError:
            body = None
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        # The class name only: an exception's text can echo the request.
        logger.info(
            "byok plan probe for plan=%s did not complete (%s)",
            backend.plan,
            type(exc).__name__,
        )
    verdict, code = _classify(status, body)
    return ModelByokPlanProbe(
        plan=backend.plan,
        http_status=status,
        provider_code=code,
        verdict=verdict,
        model=discovery.model,
    )


def _probe_rows(
    provider: str,
) -> tuple[list[ModelByokProviderBackend], list[ModelByokProviderBackend]]:
    """The routable rows (default plan first, then sorted) and the detection-only rows."""
    default = resolve_byok_provider_backend(provider)
    routable: list[ModelByokProviderBackend] = [default] if default is not None else []
    detection_only: list[ModelByokProviderBackend] = []
    for plan in byok_provider_plans(provider):
        row = resolve_byok_declared_plan(provider, plan)
        if row is None:
            continue
        if not row.customer_routable:
            detection_only.append(row)
        elif default is None or row.plan != default.plan:
            routable.append(row)
    return routable, detection_only


async def detect_byok_plan(
    provider: str,
    api_key: str | SecretStr,
    *,
    post: PostChatCompletion = post_chat_completion,
    get: GetJson | None = None,
) -> ModelByokPlanDetection:
    """Find which plan ``api_key`` belongs to for ``provider``.

    Args:
        provider: the provider id the customer is registering a key for.
        api_key: the customer's key. Sent only as the bearer credential of the
            probe; never logged, stored or returned.
        post: the contract transport call. Tests pass a fake; production uses
            the delegation effect's own transport.
        get: the list-models read each plan's probe model is resolved through
            (OMN-20157). Tests pass a fake.

    Returns:
        The detection. ``outcome`` is ``single_plan`` (no network, the
        provider declares one plan), ``not_offered`` (no such provider),
        ``detected``, ``ambiguous``, ``not_permitted`` (the key belongs only to a
        plan the provider's terms bar us from routing; carries the typed
        ``refusal_code``), ``rejected`` or ``inconclusive``.
    """
    normalized = provider.strip().lower()
    routable, detection_only = _probe_rows(normalized)
    if not routable:
        return ModelByokPlanDetection(
            provider=normalized, plan=None, outcome="not_offered"
        )
    if len(routable) + len(detection_only) == 1:
        return ModelByokPlanDetection(
            provider=normalized, plan=routable[0].plan, outcome="single_plan"
        )

    secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
    probes: list[ModelByokPlanProbe] = []
    for row in routable:
        probes.append(await asyncio.to_thread(_probe_sync, post, row, secret, get))

    answered = [probe for probe in probes if probe.verdict == "answered"]
    if len(answered) == 1:
        return ModelByokPlanDetection(
            provider=normalized,
            plan=answered[0].plan,
            outcome="detected",
            probes=tuple(probes),
            model=answered[0].model,
        )
    if len(answered) > 1:
        return ModelByokPlanDetection(
            provider=normalized,
            plan=None,
            outcome="ambiguous",
            probes=tuple(probes),
        )
    if any(probe.verdict != "rejected" for probe in probes):
        # A routable surface that could not answer might have been the right
        # one. Nothing is refused, and no detection-only surface is contacted,
        # on evidence that thin.
        return ModelByokPlanDetection(
            provider=normalized,
            plan=None,
            outcome="inconclusive",
            probes=tuple(probes),
        )

    # Every routable plan refused the key. Only now is a detection-only plan
    # tried, to tell a wrong key from a key the provider's terms bar us from
    # routing.
    refused: ModelByokProviderBackend | None = None
    for row in detection_only:
        probe = await asyncio.to_thread(_probe_sync, post, row, secret, get)
        probes.append(probe)
        if probe.verdict == "answered" and refused is None:
            refused = row
    if refused is not None:
        return ModelByokPlanDetection(
            provider=normalized,
            plan=None,
            outcome="not_permitted",
            probes=tuple(probes),
            refused_plan=refused.plan,
            refusal_code=refused.refusal_code,
        )
    outcome: Literal["rejected", "inconclusive"] = (
        "rejected"
        if all(probe.verdict == "rejected" for probe in probes)
        else "inconclusive"
    )
    return ModelByokPlanDetection(
        provider=normalized, plan=None, outcome=outcome, probes=tuple(probes)
    )


__all__: list[str] = [
    "ModelByokPlanDetection",
    "ModelByokPlanProbe",
    "detect_byok_plan",
]
