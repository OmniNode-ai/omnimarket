# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Metered hosted rungs a deployment adds to the delegation ladder (OMN-23756).

The eval arms (OMN-23751) let the code eval pin a hosted model by backend id.
A paid rung is the next step: a hosted model that ordinary routing may select,
inside a metered tier the overlay inserts into the class ladders. Which models,
in which order, at what price, and how much they may spend are deployment facts,
so the package declares only the shape and the values come from the routing
overlay bound through ``DELEGATION_ROUTING_OVERLAY_PATH``.

Properties enforced here or where the overlay is applied:

* ``enabled`` is the one switch; off (the default) inserts no tier and appends
  no backend, so a bound overlay with the switch off routes exactly as before;
* a rung is never booked at zero: its tier is either one of the overlay's own
  tiers, which are always ``metered``, or a ladder tier whose cost model is
  metered (a ``free_local`` ladder tier is refused when the ladder is placed);
* every rung backend is an ``internal`` surface, ``house`` tenant endpoint, so a
  customer route can never reach the house credential through it;
* ``spend_cap_usd`` is checked before a call leaves: once the measured spend
  since switch-on reaches it the paid tiers are skipped with a typed reason;
* a GLM model never rides a paid rung (GLM routes direct, never through an
  aggregator).

The rung record is :class:`ModelEvalArm`, the same price record the eval arms
use, so one shape carries a hosted model's slug, price and context window.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal

from omnibase_core.models.delegation.wire import (
    EnumTierCostType,
    ModelRoutingTier,
    ModelTierCost,
    ModelTierModel,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.model_eval_arms_overlay import ModelEvalArm
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendKind,
    EnumDelegationBackendSurface,
    EnumDelegationBackendTenantScope,
    ModelDelegationBackendConfig,
)

_PER_THOUSAND = Decimal(1000)
#: Slug fragments that name a GLM model. GLM never routes through an aggregator.
_GLM_SLUG_MARKERS = ("z-ai/", "glm-")


class ModelPaidTier(BaseModel):
    """One metered tier the overlay inserts into the ladder."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    max_retries: int = Field(default=1, ge=0)
    eval_before_accept: bool = True


class ModelPaidRungSpendSource(BaseModel):
    """Where the provider's own usage accounting is read, by reference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    credits_url: str = Field(..., min_length=1)
    secret_ref: str = Field(..., min_length=1)


class ModelPaidRungsOverlay(BaseModel):
    """The paid rungs a deployment declares, their switch and their spending cap."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    spend_cap_usd: Decimal = Field(..., gt=0)
    enabled_at: datetime | None = Field(
        default=None,
        description="When the switch was set; the burn reading's start.",
    )
    usage_baseline_usd: Decimal | None = Field(
        default=None,
        ge=0,
        description=(
            "The provider's total usage figure read when the switch was set; "
            "spend since switch-on is the current figure minus this one."
        ),
    )
    after_tier: str = Field(
        ...,
        min_length=1,
        description="The ladder tier the paid tiers follow, in every class order that carries it.",
    )
    spend_source: ModelPaidRungSpendSource
    tiers: tuple[ModelPaidTier, ...] = Field(..., min_length=1)
    backends: tuple[ModelDelegationBackendConfig, ...] = Field(..., min_length=1)
    rungs: tuple[ModelEvalArm, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _validate_rungs(self) -> ModelPaidRungsOverlay:
        """Report every rung, backend and switch setting that could misbook or leak."""
        problems: list[str] = []
        for label, names in (
            ("paid tier name", tuple(t.name for t in self.tiers)),
            ("paid backend_id", tuple(b.backend_id for b in self.backends)),
            ("paid rung backend_id", tuple(r.backend_id for r in self.rungs)),
        ):
            for name, count in Counter(names).items():
                if count > 1:
                    problems.append(f"duplicate {label} {name!r}")
        if self.enabled and (
            self.enabled_at is None or self.usage_baseline_usd is None
        ):
            problems.append(
                "paid_rungs.enabled needs enabled_at and usage_baseline_usd, the "
                "switch-on time and the provider usage figure read at that time"
            )
        tier_names = {t.name for t in self.tiers}
        if self.after_tier in tier_names:
            problems.append(f"after_tier {self.after_tier!r} is a paid tier")
        rungs = {r.backend_id: r for r in self.rungs}
        for backend in self.backends:
            bid = backend.backend_id
            if backend.kind is not EnumDelegationBackendKind.ENDPOINT:
                problems.append(f"paid backend {bid!r} kind must be endpoint")
            if backend.explicit_pin_only:
                problems.append(
                    f"paid backend {bid!r} is explicit_pin_only; a ladder rung "
                    "must be selectable (an eval-only model belongs in eval_arms)"
                )
            if backend.surface is not EnumDelegationBackendSurface.INTERNAL:
                problems.append(f"paid backend {bid!r} surface must be internal")
            if backend.tenant_scope is not EnumDelegationBackendTenantScope.HOUSE:
                problems.append(f"paid backend {bid!r} tenant_scope must be house")
            if not (backend.endpoint_url or "").strip():
                problems.append(f"paid backend {bid!r} needs a complete endpoint_url")
            if (backend.provider or "").lower() == "glm":
                problems.append(
                    f"paid backend {bid!r} is a GLM backend; GLM routes direct"
                )
            rung = rungs.get(bid)
            if rung is None:
                problems.append(f"paid backend {bid!r} has no rung")
                continue
            if (backend.model_name or "") != rung.slug:
                problems.append(
                    f"paid rung {bid!r} slug {rung.slug!r} differs from backend "
                    f"model_name {backend.model_name!r}"
                )
            if any(marker in rung.slug.lower() for marker in _GLM_SLUG_MARKERS):
                problems.append(f"paid rung {bid!r} slug {rung.slug!r} is a GLM model")
            extra = sorted(set(backend.capabilities) - set(rung.task_classes))
            if extra:
                problems.append(
                    f"paid backend {bid!r} declares capabilities {extra} its rung does not list"
                )
        for name in sorted(tier_names - {r.tier for r in self.rungs}):
            problems.append(f"paid tier {name!r} has no rungs")
        declared = {b.backend_id for b in self.backends}
        for rung in self.rungs:
            if rung.backend_id not in declared:
                problems.append(
                    f"paid rung names undeclared backend {rung.backend_id!r}"
                )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def tier_names(self) -> frozenset[str]:
        """The names of the overlay's own paid tiers."""
        return frozenset(t.name for t in self.tiers)

    def backend_rows(self) -> list[dict[str, object]]:
        """The rung backends as routing-contract rows; empty while the switch is off."""
        if not self.enabled:
            return []
        rows: list[dict[str, object]] = []
        for backend in self.backends:
            row = backend.model_dump(mode="json", exclude_none=True)
            # An added contract row states these keys even when null (OMN-17099).
            row.setdefault("secret_ref", None)
            row.setdefault("endpoint_url", None)
            rows.append(row)
        return rows

    def price_per_1m(
        self, endpoint_url: str | None, model_name: str
    ) -> tuple[Decimal, Decimal] | None:
        """The rung's declared ``(input, output)`` USD per 1M tokens, or ``None``."""
        if not endpoint_url:
            return None
        rungs = {r.backend_id: r for r in self.rungs}
        for backend in self.backends:
            rung = rungs.get(backend.backend_id)
            if (
                rung is not None
                and backend.endpoint_url == endpoint_url
                and rung.slug == model_name
            ):
                return rung.price_in_usd_per_mtok, rung.price_out_usd_per_mtok
        return None

    def rungs_in(self, tier_name: str) -> tuple[ModelEvalArm, ...]:
        """The rungs declared for ``tier_name``, in declaration (cheapest-first) order."""
        return tuple(r for r in self.rungs if r.tier == tier_name)

    def _tier_model(self, rung: ModelEvalArm) -> ModelTierModel:
        return ModelTierModel(
            id=rung.slug,
            backend_ref=rung.backend_id,
            max_context_tokens=rung.context_length,
            use_for=rung.task_classes,
        )

    def routing_tiers(self) -> tuple[ModelRoutingTier, ...]:
        """The overlay's paid tiers as metered ladder tiers.

        A tier's flat figure is its dearest rung's dearer price per 1k tokens, so
        a class's pricing ceiling is compared against the worst case the tier can
        bill; each call is still booked at its own rung's declared price.
        """
        tiers: list[ModelRoutingTier] = []
        for tier in self.tiers:
            rungs = self.rungs_in(tier.name)
            rate = max(
                (
                    max(r.price_in_usd_per_mtok, r.price_out_usd_per_mtok)
                    / _PER_THOUSAND
                    for r in rungs
                ),
                default=Decimal(0),
            )
            tiers.append(
                ModelRoutingTier(
                    name=tier.name,
                    models=tuple(self._tier_model(r) for r in rungs),
                    eval_before_accept=tier.eval_before_accept,
                    cost_per_1k_tokens=float(rate),
                    cost=ModelTierCost(
                        cost_type=EnumTierCostType.METERED,
                        rate_per_1k_usd=float(rate),
                    ),
                    max_retries=tier.max_retries,
                )
            )
        return tuple(tiers)

    def ladder_rungs_for(self, tier_name: str) -> tuple[ModelTierModel, ...]:
        """Rungs that join an existing ladder tier named ``tier_name``."""
        return tuple(self._tier_model(r) for r in self.rungs_in(tier_name))

    def absent_slugs(self, catalog_ids: frozenset[str]) -> list[str]:
        """Every rung slug the provider's live model list does not carry."""
        return sorted({r.slug for r in self.rungs if r.slug not in catalog_ids})


__all__: list[str] = [
    "ModelPaidRungSpendSource",
    "ModelPaidRungsOverlay",
    "ModelPaidTier",
]
