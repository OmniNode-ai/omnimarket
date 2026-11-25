# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24859: price a BYOK call from the catalogue's declared price.

The delegation projection re-prices actual cost through the serving tier's typed
cost model. A call made on a customer's own key (BYOK) serves on the
``tenant_overlay`` tier, which has no typed cost model, so the recompute books
``cost_usd=0.0`` with ``cost_measurement_source=no_cost_model`` even though the
BYOK catalogue declares a price for that backend and model. This module resolves
that declared price and converts the measured tokens to USD. Every arm that
cannot prove a priced BYOK call returns ``None``, so the caller keeps its
existing behaviour: a call on a house key is never priced here, and a model with
no declared price stays unpriced rather than booked at a tier rate.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from omnimarket.handlers.model_catalog import models_dev_price_with_read_time
from omnimarket.handlers.routing.byok_provider_backends import (
    byok_backend_provider,
    byok_model_price_with_source,
    resolve_byok_backend_by_id,
)

logger = logging.getLogger(__name__)

_PER_1M = Decimal("1000000")


def byok_priced_actual_cost(
    *,
    backend_id: str | None,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float | None:
    """Return the catalogue-priced USD cost of one BYOK call, or ``None``.

    ``None`` whenever the call is not a priced call on a customer's own key: no
    backend, a non-BYOK backend, a backend that cannot be resolved, or a model
    the catalogue prices nowhere. Otherwise the measured tokens priced at the
    declared ``(input, output)`` USD-per-1M rates, computed in ``Decimal`` and
    returned as a ``float``. One INFO line per priced call names the source that
    priced it (catalogue, model registry or models.dev) and, for models.dev, the
    time it was read, so a runtime log shows which leg of the lookup ran.
    """
    if not backend_id or byok_backend_provider(backend_id) is None:
        return None
    backend = resolve_byok_backend_by_id(backend_id)
    if backend is None:
        return None
    priced = byok_model_price_with_source(
        backend_id,
        backend.endpoint_url,
        model_name,
        models_dev_price=models_dev_price_with_read_time,
    )
    if priced is None:
        return None
    (price_in, price_out), price_source, price_read_at = priced
    logger.info(
        "BYOK call priced: backend_id=%s model=%s price_source=%s price_read_at=%s",
        backend_id,
        model_name,
        price_source,
        price_read_at,
    )
    cost = (
        Decimal(prompt_tokens) * price_in + Decimal(completion_tokens) * price_out
    ) / _PER_1M
    return float(cost)


__all__: list[str] = ["byok_priced_actual_cost"]
