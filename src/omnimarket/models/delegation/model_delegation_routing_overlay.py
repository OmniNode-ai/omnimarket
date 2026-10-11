# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Private harness routing declarations loaded through an explicit overlay."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.delegation.model_class_escalation_chain import (
    ModelClassEscalationChain,
)
from omnimarket.models.delegation.model_harness_tier import ModelHarnessTier
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


class ModelDelegationRoutingOverlay(BaseModel):
    """Harness backends, tiers and chains supplied by the operator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["delegation_routing_overlay.v1"]
    harness_backends: tuple[ModelDelegationBackendConfig, ...] = ()
    harness_tiers: tuple[ModelHarnessTier, ...] = ()
    escalation_chains: tuple[ModelTaskClassEscalationChain, ...] = ()

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


EMPTY_DELEGATION_ROUTING_OVERLAY = ModelDelegationRoutingOverlay(
    schema_version="delegation_routing_overlay.v1"
)

__all__ = [
    "EMPTY_DELEGATION_ROUTING_OVERLAY",
    "ModelDelegationRoutingOverlay",
    "ModelTaskClassEscalationChain",
]
