# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the lab host table judgement (OMN-17232).

The rules (``ModelPlacementRules``) are read from this node's ``contract.yaml``; the table rows
are the lab host table's (``ModelLabHostRow``); a unit is a repository and a work kind. Nothing
about a repository or a machine is a literal in code.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.overlay_keys import ModelLabHostRow, ModelUnitNeedsRule

#: The boolean host-table columns a unit's ``needs`` may name. Each is a field of
#: ``ModelLabHostRow``; a rule naming anything else is refused when the rules load.
CAPABILITY_COLUMNS: frozenset[str] = frozenset({"avx2", "docker"})


class ModelHostDefaults(BaseModel):
    """What a host row that omits a column means."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    avx2: bool
    docker: bool
    max_weight: str
    speed: float = Field(..., gt=0)


class ModelPlacementRules(BaseModel):
    """The ``placement`` block of the node contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    weights: tuple[str, ...] = Field(..., min_length=1)
    weight_by_kind: dict[str, str]
    host_defaults: ModelHostDefaults
    unit_needs: tuple[ModelUnitNeedsRule, ...] = ()

    def check_needs(self, rules: tuple[ModelUnitNeedsRule, ...]) -> None:
        """Raise ``ValueError`` for a rule naming anything that is not a host capability column."""
        for rule in rules:
            unknown = sorted(set(rule.needs) - CAPABILITY_COLUMNS)
            if unknown:
                raise ValueError(
                    f"unit needs for {rule.repo}: {unknown} are not host "
                    f"capability columns {sorted(CAPABILITY_COLUMNS)}"
                )

    @model_validator(mode="after")
    def _consistent(self) -> ModelPlacementRules:
        if len(set(self.weights)) != len(self.weights):
            raise ValueError("placement.weights repeats a weight")
        known = set(self.weights)
        if self.host_defaults.max_weight not in known:
            raise ValueError(
                f"placement.host_defaults.max_weight {self.host_defaults.max_weight!r} "
                f"is not one of {list(self.weights)}"
            )
        for kind, weight in self.weight_by_kind.items():
            if weight not in known:
                raise ValueError(
                    f"placement.weight_by_kind[{kind}] {weight!r} is not one of "
                    f"{list(self.weights)}"
                )
        self.check_needs(self.unit_needs)
        return self

    def weight_rank(self, weight: str) -> int:
        """Position of ``weight``, lightest first; raises ``ValueError`` for a weight the rules do not name."""
        if weight not in self.weights:
            raise ValueError(f"weight {weight!r} is not one of {list(self.weights)}")
        return self.weights.index(weight)


class ModelUnitProfile(BaseModel):
    """What one unit asks of a host, derived from its repository and kind."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    kind: str
    weight: str
    needs: tuple[str, ...]
    need_reasons: tuple[str, ...]


class ModelHostTableVerdict(BaseModel):
    """Whether the host table lets one host take the unit, and the host's own memory bar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    eligible: bool
    reason: str
    #: The row's own memory admission bar in GB, replacing the caller's bar; None: the caller's.
    min_free_gb: float | None = None
    speed: float = 1.0


class ModelLabPlacementRequest(BaseModel):
    """One unit and the hosts to judge for it against the host table rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="owner/name or the bare repository name")
    kind: str = "other"
    weight: str | None = None
    host_names: tuple[str, ...]
    table: tuple[ModelLabHostRow, ...]
    #: What units of named repositories need of a host, from the table document; added to the contract's.
    unit_needs: tuple[ModelUnitNeedsRule, ...] = ()
    #: The tools the caller asked for. One that names a capability column is judged as a need.
    need_tools: tuple[str, ...] = ()


class ModelLabPlacementResult(BaseModel):
    """The unit's profile and one verdict per distinct host name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: ModelUnitProfile
    verdicts: tuple[ModelHostTableVerdict, ...]

    def by_host(self) -> dict[str, ModelHostTableVerdict]:
        return {verdict.host_name: verdict for verdict in self.verdicts}
