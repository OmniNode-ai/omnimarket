# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A run's recorded cost as spend, only from a named price source.

OMN-24615 AC3, decisions D1 and D3. A stored cost is spend only when the
pricing manifest prices the model that ran, and a positive cost on a model the
manifest lists as free is a routing tier's flat rate, not a price. Any other
run is unpriced (None): the metering fold counts it in runs_unknown_spend and
gives it no saving. Stored rows are classified here, at read time, and never
rewritten (D1).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from functools import cache
from typing import TYPE_CHECKING

from omnibase_core.enums.enum_tier_cost_type import EnumTierCostType

from omnimarket.pricing import _load_table, resolve_tier_cost

if TYPE_CHECKING:
    from omnimarket.handlers.model_catalog import ModelsDevIndex

__all__ = [
    "historical_unpriced_zero",
    "historical_unpriced_zero_reader",
    "priced_spend",
]

#: Returns the models.dev index a stored run may be priced from (AC35).
ModelsDevReader = Callable[[], "ModelsDevIndex"]


def historical_unpriced_zero(
    amount: object,
    source: object,
    tier: object,
    tier_type: object,
    attempts: object,
    *models: object,
    models_dev: ModelsDevReader | None = None,
) -> bool:
    """True for a stored zero that no price stood behind. Never reprices a run.

    A charged amount is served as stored. A rejected attempt the provider
    answered (so billed) that is itself an unpriced zero makes the whole run's
    cost unknown, whatever the answering attempt cost (operator ruling
    23413-G6). Otherwise a cost source other than no_cost_model is served as
    stored. The row's own record comes first: a recorded free_local tier type
    is free, and a recorded no_cost_model source or non-free tier type is not
    made free by today's routing registry. Only a row that records neither is
    read against the registry, and only a tier it declares free_local is free;
    a tier with no cost model is not a free tier. A row that records no tier,
    tier type or source, or names no model, is served as stored, since nothing
    in it shows a priced cloud call. Otherwise the run is free only when its
    model id ends in ":free", or the pricing manifest, the provider catalogue
    for the backend the row names, or models.dev read live for that backend's
    provider (``models_dev``, AC35; never the shipped price list) declares its
    price zero.
    """
    if not _is_zero(amount):
        return False
    if _billed_unpriced_attempt(attempts, models_dev):
        return True
    recorded_source = str(source or "").strip()
    tier_cost_type = str(tier_type or "").strip().casefold()
    if recorded_source not in ("", "no_cost_model"):
        return False
    if tier_cost_type == EnumTierCostType.FREE_LOCAL.value:
        return False
    if not recorded_source and not tier_cost_type:
        tier_name = str(tier or "").strip().casefold()
        if not tier_name:
            return False
        cost = resolve_tier_cost(tier_name)
        if cost is not None and cost.cost_type is EnumTierCostType.FREE_LOCAL:
            return False
    # Every name the row records is read: delegated_to can be a backend alias
    # while routed_model names the model that answered.
    names = [str(m).strip() for m in models if m is not None and str(m).strip()]
    if not names:
        return False
    if any(_declared_free(name) for name in names):
        return False
    return not any(
        _catalogue_declares_free(backend, model)
        or _models_dev_declares_free(backend, model, models_dev)
        for backend in names
        for model in names
        if backend != model
    )


def historical_unpriced_zero_reader() -> Callable[..., bool]:
    """``historical_unpriced_zero`` for one read, with models.dev read at most once.

    The live models.dev read (``models_dev_historical_read``) happens only
    when a row names a backend whose provider models.dev lists.
    """
    from omnimarket.handlers import models_dev_historical_read

    @cache
    def models_dev() -> ModelsDevIndex:
        return models_dev_historical_read.historical_models_dev_index()

    def read(amount: object, *columns: object) -> bool:
        return historical_unpriced_zero(amount, *columns, models_dev=models_dev)

    return read


def _is_zero(amount: object) -> bool:
    if amount is None or isinstance(amount, bool):
        return False
    try:
        return Decimal(str(amount)) == 0
    except (InvalidOperation, ValueError):
        return False


def _billed_unpriced_attempt(
    attempts: object, models_dev: ModelsDevReader | None
) -> bool:
    """True when a rejected attempt the provider answered is an unpriced zero.

    The answering attempt is the row's own record and is judged from it. An
    attempt with no answer (a refusal, a timeout, a skipped rung) was not
    billed. A stored history that is not a list of attempts is ignored.
    """
    if not isinstance(attempts, str) or not attempts.strip():
        return False
    try:
        ladder = json.loads(attempts)
    except ValueError:
        return False
    if not isinstance(ladder, list):
        return False
    for attempt in ladder:
        if not isinstance(attempt, Mapping):
            continue
        if attempt.get("quality_gate_passed") is True or (
            attempt.get("acceptance_decision") == "accept"
        ):
            continue
        status = attempt.get("http_status")
        answered = (
            isinstance(status, int)
            and not isinstance(status, bool)
            and 200 <= status < 300
        ) or bool(attempt.get("finish_reason"))
        if answered and historical_unpriced_zero(
            attempt.get("cost_usd", 0.0),
            None,
            attempt.get("tier"),
            None,
            None,
            attempt.get("model_id"),
            attempt.get("backend_id"),
            models_dev=models_dev,
        ):
            return True
    return False


@cache
def _catalogue_declares_free(backend_id: str, model: str) -> bool:
    """True when the provider catalogue declares ``model`` free on ``backend_id``.

    The catalogue's declared price, then the model registry's, as a call on that
    backend books it, but never the shipped models.dev price list.
    """
    from omnimarket.handlers.routing.byok_provider_backends import (
        byok_model_price_per_1m,
        load_byok_plan_catalog,
    )

    for row in load_byok_plan_catalog().values():
        if row.backend_id == backend_id:
            price = byok_model_price_per_1m(
                backend_id, row.endpoint_url, model, supplemental=False
            )
            return price is not None and price[0] == 0 and price[1] == 0
    return False


def _models_dev_declares_free(
    backend_id: str, model: str, models_dev: ModelsDevReader | None
) -> bool:
    """True when models.dev, read live, lists ``model`` free for the backend's provider.

    OMN-23413 AC35: with no price from the provider, models.dev's price for that
    provider and model. The index is read only for a backend whose provider
    models.dev lists.
    """
    if models_dev is None:
        return False
    from omnimarket.handlers import model_catalog
    from omnimarket.handlers.routing.byok_provider_backends import (
        byok_backend_provider,
    )

    provider = byok_backend_provider(backend_id)
    if provider is None or not model_catalog.catalog_config()["provider_ids"].get(
        provider
    ):
        return False
    price = model_catalog.models_dev_index_price_per_1m(models_dev(), provider, model)
    return price is not None and price[0] == 0 and price[1] == 0


def _declared_free(name: str) -> bool:
    """A ":free" model id, or one the pricing manifest declares at a zero price.

    A positive price does not make a stored zero free, and the run is not
    repriced here.
    """
    if name.casefold().endswith(":free"):
        return True
    table = _load_table()
    entry = table.get_entry(name) or table.get_entry(name.lower())
    return (
        entry is not None
        and entry.input_cost_per_1k == 0
        and entry.output_cost_per_1k == 0
    )


def priced_spend(row: Mapping[str, object]) -> Decimal | None:
    """The row's ``cost_usd`` as spend, or None when no named price stands behind it."""
    raw = row.get("cost_usd")
    if raw is None:
        return None
    try:
        spend = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    model = str(row.get("model_name") or row.get("delegated_to") or "")
    # The manifest keys some models in lower case (qwen3.6-35b-a3b) while runs
    # record the served name (Qwen3.6-35B-A3B).
    table = _load_table()
    entry = table.get_entry(model) or table.get_entry(model.lower())
    if entry is None:
        return None
    free = entry.input_cost_per_1k == 0 and entry.output_cost_per_1k == 0
    return None if free and spend > 0 else spend
