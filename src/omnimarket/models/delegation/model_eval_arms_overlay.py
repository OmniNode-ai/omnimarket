# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Eval-only endpoint arms carried by the delegation routing overlay (OMN-23751).

An eval arm is a hosted model the code eval may pin by backend id before the
eval has scored it. Which models those are, what they cost and how much an
eval may spend are deployment facts, so the package declares only the shape;
the values come from the routing overlay a deployment binds through
``DELEGATION_ROUTING_OVERLAY_PATH``.

Three properties keep an arm off real lane work, each enforced here:

* every arm's backend is ``explicit_pin_only``, so neither the tier ladder nor
  the capability fallback ever selects it; only a run that names its id does;
* no arm backend has a ``placement``, so no tier is appended for it;
* every arm backend is ``surface: internal`` and ``tenant_scope: house``, so a
  customer route can never reach a house credential through it.

The spend cap is enforced by :meth:`ModelEvalArmsOverlay.admit_run`, which an
eval run calls before its first try: a run whose projected cost exceeds what is
left of the cap is refused, never started.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendKind,
    EnumDelegationBackendSurface,
    EnumDelegationBackendTenantScope,
    ModelDelegationBackendConfig,
)

_PER_MILLION = Decimal(1_000_000)


class ModelEvalArm(BaseModel):
    """One eval arm: the model a backend serves, its tier, price and classes."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    backend_id: str = Field(..., min_length=1)
    slug: str = Field(
        ...,
        min_length=1,
        description="The provider's model id; must equal the backend's model_name.",
    )
    tier: str = Field(..., min_length=1)
    price_in_usd_per_mtok: Decimal = Field(..., ge=0)
    price_out_usd_per_mtok: Decimal = Field(..., ge=0)
    context_length: int = Field(..., gt=0)
    task_classes: tuple[str, ...] = Field(..., min_length=1)
    catalog_read_at: datetime = Field(
        ...,
        description="When the slug, price and context length were read from the provider's live catalog.",
    )

    def projected_cost_usd(self, tokens_in: int, tokens_out: int) -> Decimal:
        """The cost of ``tokens_in`` prompt and ``tokens_out`` completion tokens."""
        if tokens_in < 0 or tokens_out < 0:
            msg = "token counts must not be negative"
            raise ValueError(msg)
        return (
            Decimal(tokens_in) * self.price_in_usd_per_mtok
            + Decimal(tokens_out) * self.price_out_usd_per_mtok
        ) / _PER_MILLION


class ModelEvalSpendAdmission(BaseModel):
    """The answer to "may this eval run start" against the spend cap."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool
    backend_id: str
    projected_cost_usd: Decimal
    remaining_usd: Decimal
    reason: str


class ModelEvalArmsOverlay(BaseModel):
    """The eval arms a deployment declares, and the cap on what evals may spend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spend_cap_usd: Decimal = Field(..., gt=0)
    backends: tuple[ModelDelegationBackendConfig, ...] = Field(..., min_length=1)
    arms: tuple[ModelEvalArm, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _validate_arms(self) -> ModelEvalArmsOverlay:
        """Report every arm and backend that could leak onto real work."""
        problems: list[str] = []
        for label, names in (
            ("eval backend_id", tuple(b.backend_id for b in self.backends)),
            ("eval arm backend_id", tuple(a.backend_id for a in self.arms)),
        ):
            for name, count in Counter(names).items():
                if count > 1:
                    problems.append(f"duplicate {label} {name!r}")
        arms = {a.backend_id: a for a in self.arms}
        for backend in self.backends:
            bid = backend.backend_id
            if backend.kind is not EnumDelegationBackendKind.ENDPOINT:
                problems.append(f"eval backend {bid!r} kind must be endpoint")
            if not backend.explicit_pin_only:
                problems.append(f"eval backend {bid!r} must be explicit_pin_only")
            if backend.surface is not EnumDelegationBackendSurface.INTERNAL:
                problems.append(f"eval backend {bid!r} surface must be internal")
            if backend.tenant_scope is not EnumDelegationBackendTenantScope.HOUSE:
                problems.append(f"eval backend {bid!r} tenant_scope must be house")
            if not (backend.endpoint_url or "").strip():
                problems.append(f"eval backend {bid!r} needs a complete endpoint_url")
            arm = arms.get(bid)
            if arm is None:
                problems.append(f"eval backend {bid!r} has no arm")
                continue
            if (backend.model_name or "") != arm.slug:
                problems.append(
                    f"eval arm {bid!r} slug {arm.slug!r} differs from backend "
                    f"model_name {backend.model_name!r}"
                )
            extra = sorted(set(backend.capabilities) - set(arm.task_classes))
            if extra:
                problems.append(
                    f"eval backend {bid!r} declares capabilities {extra} its arm does not list"
                )
        declared = {b.backend_id for b in self.backends}
        for arm in self.arms:
            if arm.backend_id not in declared:
                problems.append(f"eval arm names undeclared backend {arm.backend_id!r}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def arm_by_backend_id(self) -> dict[str, ModelEvalArm]:
        """Index the arms by the backend id a run pins."""
        return {arm.backend_id: arm for arm in self.arms}

    def admit_run(
        self,
        backend_id: str,
        projected_tokens_in: int,
        projected_tokens_out: int,
        spent_usd: Decimal,
    ) -> ModelEvalSpendAdmission:
        """Admit or refuse an eval run on ``backend_id`` against the spend cap.

        ``spent_usd`` is what earlier eval runs have already spent against this
        cap. The run is refused when its projected cost exceeds what is left,
        and an unknown backend id is refused rather than admitted at no cost.
        """
        if spent_usd < 0:
            msg = "spent_usd must not be negative"
            raise ValueError(msg)
        remaining = max(self.spend_cap_usd - spent_usd, Decimal(0))
        arm = self.arm_by_backend_id().get(backend_id)
        if arm is None:
            return ModelEvalSpendAdmission(
                admitted=False,
                backend_id=backend_id,
                projected_cost_usd=Decimal(0),
                remaining_usd=remaining,
                reason=f"{backend_id!r} is not an eval arm",
            )
        projected = arm.projected_cost_usd(projected_tokens_in, projected_tokens_out)
        if projected > remaining:
            return ModelEvalSpendAdmission(
                admitted=False,
                backend_id=backend_id,
                projected_cost_usd=projected,
                remaining_usd=remaining,
                reason=(
                    f"projected cost {projected} USD exceeds the {remaining} USD "
                    f"left of the {self.spend_cap_usd} USD eval spend cap"
                ),
            )
        return ModelEvalSpendAdmission(
            admitted=True,
            backend_id=backend_id,
            projected_cost_usd=projected,
            remaining_usd=remaining,
            reason="within the eval spend cap",
        )


__all__: list[str] = [
    "ModelEvalArm",
    "ModelEvalArmsOverlay",
    "ModelEvalSpendAdmission",
]
