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
For each plan the catalogue declares for the provider it POSTs a one-token
completion to that plan's declared endpoint with the customer's key and reads
the answer:

* ``answered``: the key authenticated on that product.
* ``rejected``: the product refused the key (401/403, or z.ai 1113, the
  pay-as-you-go surface refusing a Coding-Plan key).
* ``inconclusive``: anything else (a throttle, a 5xx, a capacity code, a network
  failure). Not evidence about the key, so it is never read as a rejection.

Every plan is probed, because the answer that matters is whether ONE plan or
SEVERAL accept the key:

* exactly one answered: ``detected``, that plan. A plan that was rejected or
  could not be reached is not evidence the key also belongs there.
* several answered: ``ambiguous``, no plan. The key works on more than one
  product and the products meter differently, so choosing for the customer is a
  guess about whose money is spent. The caller asks the customer to name it.
  (The platform's own z.ai key is such a key: it answers on both surfaces.)
* none answered: ``rejected`` when every plan rejected the key, otherwise
  ``inconclusive``, because a surface that could not be reached might have been
  the right one.

Detection never guesses.

An exhausted window (z.ai 1308, 1310, 1316, 1317) counts as ``answered``: the
key authenticated on that product and is capped, which is the plan.

What it does not do
-------------------
It never logs or returns the key, and the result carries only statuses and
provider codes. Provider-specific reading of the response is confined to the
small tables below; the quota mechanics that interpret the same codes at
delegation time are owned by OMN-20154.

The probe costs at most a one-token completion per plan, each against the plan's
declared default model. On a pay-as-you-go plan that is a free model.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from omnimarket.routing.byok_provider_backends import (
    ModelByokProviderBackend,
    byok_provider_plans,
    load_byok_plan_catalog,
    resolve_byok_provider_backend,
)

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 20.0

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


class ModelByokPlanDetection(BaseModel):
    """The result of detecting which plan a customer's key belongs to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    plan: str | None
    outcome: Literal[
        "detected",
        "ambiguous",
        "single_plan",
        "rejected",
        "inconclusive",
        "not_offered",
    ]
    probes: tuple[ModelByokPlanProbe, ...] = ()


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


async def _probe(
    client: httpx.AsyncClient, backend: ModelByokProviderBackend, api_key: str
) -> ModelByokPlanProbe:
    status: int | None = None
    body: Any = None
    try:
        response = await client.post(
            backend.endpoint_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": backend.model_name,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            },
        )
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
    except httpx.HTTPError as exc:
        # The class name only: an exception's text can echo the request.
        logger.info(
            "byok plan probe for plan=%s did not complete (%s)",
            backend.plan,
            type(exc).__name__,
        )
    verdict, code = _classify(status, body)
    return ModelByokPlanProbe(
        plan=backend.plan, http_status=status, provider_code=code, verdict=verdict
    )


def _probe_order(provider: str) -> list[ModelByokProviderBackend]:
    """Default plan first, then the rest in sorted order."""
    default = resolve_byok_provider_backend(provider)
    ordered: list[ModelByokProviderBackend] = [default] if default is not None else []
    catalogue = load_byok_plan_catalog()
    for plan in byok_provider_plans(provider):
        row = catalogue[(provider.strip().lower(), plan)]
        if default is None or row.plan != default.plan:
            ordered.append(row)
    return ordered


async def detect_byok_plan(
    provider: str,
    api_key: str | SecretStr,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelByokPlanDetection:
    """Find which plan ``api_key`` belongs to for ``provider``.

    Args:
        provider: the provider id the customer is registering a key for.
        api_key: the customer's key. Sent only as the bearer credential of the
            probe; never logged, stored or returned.
        transport: an HTTPX transport, for tests. Production passes ``None``.

    Returns:
        The detection. ``outcome`` is ``single_plan`` (no network, the
        provider declares one plan), ``not_offered`` (no such provider),
        ``detected``, ``rejected`` or ``inconclusive``.
    """
    normalized = provider.strip().lower()
    rows = _probe_order(normalized)
    if not rows:
        return ModelByokPlanDetection(
            provider=normalized, plan=None, outcome="not_offered"
        )
    if len(rows) == 1:
        return ModelByokPlanDetection(
            provider=normalized, plan=rows[0].plan, outcome="single_plan"
        )

    secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
    probes: list[ModelByokPlanProbe] = []
    async with httpx.AsyncClient(
        timeout=_PROBE_TIMEOUT_SECONDS, transport=transport
    ) as client:
        for row in rows:
            probes.append(await _probe(client, row, secret))

    answered = [probe.plan for probe in probes if probe.verdict == "answered"]
    if len(answered) == 1:
        return ModelByokPlanDetection(
            provider=normalized,
            plan=answered[0],
            outcome="detected",
            probes=tuple(probes),
        )
    outcome: Literal["ambiguous", "rejected", "inconclusive"]
    if len(answered) > 1:
        outcome = "ambiguous"
    elif all(probe.verdict == "rejected" for probe in probes):
        outcome = "rejected"
    else:
        outcome = "inconclusive"
    return ModelByokPlanDetection(
        provider=normalized, plan=None, outcome=outcome, probes=tuple(probes)
    )


__all__: list[str] = [
    "ModelByokPlanDetection",
    "ModelByokPlanProbe",
    "detect_byok_plan",
]
