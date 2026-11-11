# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Deployment routing declarations loaded through an explicit overlay."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Literal

from omnibase_core.models.delegation.wire import ModelRoutingTier
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.enums.enum_deployment_fact_kind import EnumDeploymentFactKind
from omnimarket.models.delegation.model_class_escalation_chain import (
    ModelClassEscalationChain,
)
from omnimarket.models.delegation.model_deployment_fact_marker import deployment_fact
from omnimarket.models.delegation.model_eval_arms_overlay import ModelEvalArmsOverlay
from omnimarket.models.delegation.model_harness_tier import ModelHarnessTier
from omnimarket.models.delegation.model_paid_rungs_overlay import (
    ModelPaidRungsOverlay,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendKind,
    ModelDelegationBackendConfig,
)


class ModelTaskClassEscalationChain(BaseModel):
    """An ordered acceptance-check chain for one task class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(..., min_length=1)
    escalate_on: Literal["acceptance_check"]
    rungs: tuple[str, ...] = Field(..., min_length=1)

    @field_validator("rungs")
    @classmethod
    def _unique_rungs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            msg = f"escalation_chain.rungs must not contain duplicates, got {value}"
            raise ValueError(msg)
        return value

    def as_chain(self) -> ModelClassEscalationChain:
        """Return the class-independent chain consumed by the resolver."""
        return ModelClassEscalationChain(escalate_on=self.escalate_on, rungs=self.rungs)


class ModelRegisteredByokTier(ModelRoutingTier):
    """A deployment's insertion point for locally registered customer responders."""

    name: str = Field(
        ...,
        min_length=1,
        json_schema_extra=deployment_fact(EnumDeploymentFactKind.ROUTING_TIER),
    )
    cost_per_1k_tokens: float = Field(..., ge=0.0)
    after_tier: str = Field(
        ...,
        min_length=1,
        json_schema_extra=deployment_fact(EnumDeploymentFactKind.ROUTING_TIER),
    )

    @model_validator(mode="after")
    def _only_registered_models(self) -> ModelRegisteredByokTier:
        if self.models:
            raise ValueError(
                "registered_byok_tier models come only from local registrations"
            )
        if self.name == self.after_tier:
            raise ValueError("registered_byok_tier cannot follow itself")
        return self


class ModelDelegationRoutingOverlay(BaseModel):
    """Harness backends, tiers and chains supplied by the operator.

    ``eval_arms`` (OMN-23751) carries the hosted models the code eval may pin
    before it has scored them, with their prices and the eval spend cap. Their
    backends are pinned-only and are appended to the loaded routing contract by
    the bifrost loaders, so a backend-id pin resolves them and nothing else does.

    ``paid_rungs`` (OMN-23756) carries hosted models ordinary routing may select:
    metered tiers inserted into the class ladders after a named tier, behind one
    switch (off by default) and a spending cap.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["delegation_routing_overlay.v1"]
    harness_backends: tuple[ModelDelegationBackendConfig, ...] = ()
    harness_tiers: tuple[ModelHarnessTier, ...] = ()
    escalation_chains: tuple[ModelTaskClassEscalationChain, ...] = ()
    registered_byok_tier: ModelRegisteredByokTier | None = None
    eval_arms: ModelEvalArmsOverlay | None = None
    paid_rungs: ModelPaidRungsOverlay | None = None

    @model_validator(mode="after")
    def _validate_references(self) -> ModelDelegationRoutingOverlay:
        """Report all duplicate declarations and invalid harness references."""
        problems: list[str] = []
        for backend in self.harness_backends:
            if backend.kind is not EnumDelegationBackendKind.HARNESS:
                problems.append(f"backend {backend.backend_id!r} kind is not harness")
        for label, names in (
            ("backend_id", tuple(b.backend_id for b in self.harness_backends)),
            ("tier name", tuple(t.name for t in self.harness_tiers)),
            ("task_class", tuple(c.task_class for c in self.escalation_chains)),
        ):
            for name, count in Counter(names).items():
                if count > 1:
                    problems.append(f"duplicate {label} {name!r}")
        backend_ids = {
            b.backend_id
            for b in self.harness_backends
            if b.kind is EnumDelegationBackendKind.HARNESS
        }
        for tier in self.harness_tiers:
            if tier.backend_id not in backend_ids:
                problems.append(
                    f"tier {tier.name!r} names undeclared harness backend {tier.backend_id!r}"
                )
        if self.eval_arms is not None:
            for backend in self.eval_arms.backends:
                if backend.backend_id in backend_ids:
                    problems.append(
                        f"eval backend {backend.backend_id!r} reuses a harness backend_id"
                    )
            eval_tiers = {arm.tier for arm in self.eval_arms.arms}
            for chain in self.escalation_chains:
                for rung in sorted(eval_tiers.intersection(chain.rungs)):
                    problems.append(
                        f"task_class {chain.task_class!r} chain names eval tier {rung!r}; "
                        "eval arms are reached only by an explicit pin"
                    )
        if self.paid_rungs is not None:
            eval_ids = (
                {b.backend_id for b in self.eval_arms.backends}
                if self.eval_arms is not None
                else set()
            )
            for backend in self.paid_rungs.backends:
                if backend.backend_id in backend_ids | eval_ids:
                    problems.append(
                        f"paid backend {backend.backend_id!r} reuses a harness or eval backend_id"
                    )
            harness_tier_names = {t.name for t in self.harness_tiers}
            for name in sorted(self.paid_rungs.tier_names() & harness_tier_names):
                problems.append(f"paid tier {name!r} reuses a harness tier name")
            if (
                self.registered_byok_tier is not None
                and self.registered_byok_tier.name in self.paid_rungs.tier_names()
            ):
                problems.append(
                    f"paid tier {self.registered_byok_tier.name!r} reuses the registered_byok_tier name"
                )
            for chain in self.escalation_chains:
                for rung in sorted(
                    self.paid_rungs.tier_names().intersection(chain.rungs)
                ):
                    problems.append(
                        f"task_class {chain.task_class!r} chain names paid tier {rung!r}; "
                        "paid tiers join the ladder through paid_rungs.after_tier"
                    )
        for chain in self.escalation_chains:
            for tier in self.harness_tiers:
                if tier.name in chain.rungs and chain.task_class not in tier.use_for:
                    problems.append(
                        f"task_class {chain.task_class!r} rung {tier.name!r} omits the class from use_for"
                    )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def chain_for(self, task_class: str) -> ModelClassEscalationChain | None:
        """Return the declared chain, if any, for a task class."""
        return next(
            (
                c.as_chain()
                for c in self.escalation_chains
                if c.task_class == task_class
            ),
            None,
        )

    def tier_by_name(self) -> dict[str, ModelHarnessTier]:
        """Index the declared harness tiers."""
        return {tier.name: tier for tier in self.harness_tiers}

    def backend_by_id(self) -> dict[str, ModelDelegationBackendConfig]:
        """Index the declared harness backends."""
        return {backend.backend_id: backend for backend in self.harness_backends}

    def eval_backend_rows(self) -> list[dict[str, object]]:
        """The eval arms' backends as routing-contract rows, empty when none."""
        if self.eval_arms is None:
            return []
        rows: list[dict[str, object]] = []
        for backend in self.eval_arms.backends:
            row = backend.model_dump(mode="json", exclude_none=True)
            # An added contract row states these keys even when null (OMN-17099).
            row.setdefault("secret_ref", None)
            row.setdefault("endpoint_url", None)
            rows.append(row)
        return rows

    def paid_rung_backend_rows(self) -> list[dict[str, object]]:
        """The paid rungs' backends as contract rows; empty when absent or switched off."""
        if self.paid_rungs is None:
            return []
        return self.paid_rungs.backend_rows()

    def declared_price_per_1m(
        self, endpoint_url: str | None, model_name: str
    ) -> tuple[Decimal, Decimal] | None:
        """The price this overlay declares for a hosted model, or ``None``.

        A paid rung's price is read first, then an eval arm's. Both are facts
        about the model, so the lookup does not depend on the paid switch: a
        call on either is booked at the overlay's price for that endpoint and
        model, never at the rate of the tier it ran in.
        """
        if self.paid_rungs is not None:
            price = self.paid_rungs.price_per_1m(endpoint_url, model_name)
            if price is not None:
                return price
        return self.eval_arm_price_per_1m(endpoint_url, model_name)

    def eval_arm_price_per_1m(
        self, endpoint_url: str | None, model_name: str
    ) -> tuple[Decimal, Decimal] | None:
        """The eval arm's declared ``(input, output)`` USD per 1M tokens, or ``None``.

        Keyed on the endpoint and the served model together, as the BYOK price
        lookup is, so a call on an eval arm is booked at the arm's own price
        and never at the rate of the ladder tier whose name it shares.
        """
        if self.eval_arms is None or not endpoint_url:
            return None
        arms = self.eval_arms.arm_by_backend_id()
        for backend in self.eval_arms.backends:
            arm = arms.get(backend.backend_id)
            if (
                arm is not None
                and backend.endpoint_url == endpoint_url
                and arm.slug == model_name
            ):
                return arm.price_in_usd_per_mtok, arm.price_out_usd_per_mtok
        return None


EMPTY_DELEGATION_ROUTING_OVERLAY = ModelDelegationRoutingOverlay(
    schema_version="delegation_routing_overlay.v1"
)

__all__ = [
    "EMPTY_DELEGATION_ROUTING_OVERLAY",
    "ModelDelegationRoutingOverlay",
    "ModelRegisteredByokTier",
    "ModelTaskClassEscalationChain",
]
