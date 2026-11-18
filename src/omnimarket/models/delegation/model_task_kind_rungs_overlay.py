# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Per-task-kind rungs the delegation routing overlay declares (OMN-24296).

A route set is one question a delegation answers (a ``task_kind``, e.g. a CI
red triage) inside one task class. Which of its kinds stay on the local GPU
and which stay on their current rung — the class's escalation chain — is a
deployment fact, so the package declares only the shape; the values come from
the routing overlay bound through ``DELEGATION_ROUTING_OVERLAY_PATH``. An
absent section changes nothing.

Properties enforced here or where the overlay is applied:

* a kind whose rung is ``local`` must have measured at least the route's
  ``min_f1``: a kind that has not earned the local rung is refused, naming the
  kind and its f1; a ``current`` kind is held to no f1 floor because it stays
  where the class's chain already puts it;
* the guard's measurement is coherent: ``measured_correct`` can never exceed
  ``measured_n``;
* no two route sets answer the same ``(task_class, task_kind)`` pair.

The routing overlay's reference check additionally refuses a route whose
``task_class`` has no escalation chain and whose ``local_backend_id`` is not a
declared local endpoint.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumTaskKindRung(StrEnum):
    """The rung a task kind is pinned to.

    ``CURRENT`` means the kind stays on its current rung: the task class's
    escalation chain decides when it leaves the local GPU.
    """

    LOCAL = "local"
    CURRENT = "current"


class ModelTaskKindRungValue(BaseModel):
    """One kind's declared rung, its input shape and its measured statistics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung: EnumTaskKindRung
    input: Literal["facts", "excerpt"]
    n: int = Field(..., gt=0)
    f1: float = Field(..., ge=0, le=1)
    precision: float | None = Field(default=None, ge=0, le=1)


class ModelTaskKindGuardSettings(BaseModel):
    """The evidence a kind's measured accuracy rests on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_samples: int = Field(..., ge=1)
    accuracy_lower_bound: float = Field(..., gt=0, le=1)
    measured_correct: int = Field(..., ge=0)
    measured_n: int = Field(..., gt=0)

    @model_validator(mode="after")
    def _validate_measurements(self) -> ModelTaskKindGuardSettings:
        """Report every incoherent measurement."""
        problems: list[str] = []
        if self.measured_correct > self.measured_n:
            problems.append(
                f"guard measured_correct {self.measured_correct} exceeds "
                f"measured_n {self.measured_n}"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self


class ModelTaskKindRouteSet(BaseModel):
    """One question's per-kind rung declarations and its local backend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(
        ...,
        min_length=1,
        description=(
            "The delegation task class whose escalation chain is the current rung."
        ),
    )
    task_kind: str = Field(
        ...,
        min_length=1,
        description="The name of the question, e.g. a CI red triage.",
    )
    local_backend_id: str = Field(..., min_length=1)
    min_f1: float = Field(..., gt=0, le=1)
    guard: ModelTaskKindGuardSettings
    kinds: dict[str, ModelTaskKindRungValue] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _validate_kinds(self) -> ModelTaskKindRouteSet:
        """Report every kind that is unnamed or has not earned the local rung."""
        problems: list[str] = []
        for kind, value in self.kinds.items():
            if not kind.strip():
                problems.append("a kind name is empty")
            if value.rung is EnumTaskKindRung.LOCAL and value.f1 < self.min_f1:
                problems.append(
                    f"kind {kind!r} rung local f1 {value.f1} is below "
                    f"min_f1 {self.min_f1}"
                )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def rung_for(self, kind: str) -> EnumTaskKindRung | None:
        """Return the declared rung for a kind, or ``None`` for an unknown kind."""
        value = self.kinds.get(kind)
        return value.rung if value is not None else None


class ModelTaskKindRungsOverlay(BaseModel):
    """The per-task-kind routes a deployment declares."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    routes: tuple[ModelTaskKindRouteSet, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _validate_routes(self) -> ModelTaskKindRungsOverlay:
        """Report every duplicate (task_class, task_kind) route set."""
        problems: list[str] = []
        seen: set[tuple[str, str]] = set()
        for route in self.routes:
            pair = (route.task_class, route.task_kind)
            if pair in seen:
                problems.append(
                    f"duplicate task_kind route for task_class {route.task_class!r} "
                    f"task_kind {route.task_kind!r}"
                )
            seen.add(pair)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def route_for(
        self, task_class: str, task_kind: str
    ) -> ModelTaskKindRouteSet | None:
        """Return the declared route, if any, for a task class and kind."""
        return next(
            (
                route
                for route in self.routes
                if route.task_class == task_class and route.task_kind == task_kind
            ),
            None,
        )


__all__: list[str] = [
    "EnumTaskKindRung",
    "ModelTaskKindGuardSettings",
    "ModelTaskKindRouteSet",
    "ModelTaskKindRungValue",
    "ModelTaskKindRungsOverlay",
]
