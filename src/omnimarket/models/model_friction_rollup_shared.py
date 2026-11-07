# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared friction rollup request models, used by the rollup node and the floor publisher."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelFrictionRollupSource(BaseModel):
    """One source in live-ledger, sorted-archive, then JSON-lines input order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    display: str
    kind: Literal["markdown", "jsonl", "missing"]
    text: str | None

    @model_validator(mode="after")
    def validate_text(self) -> Self:
        if self.kind == "missing":
            if self.text is not None:
                raise ValueError("missing source must have text=None")
        elif self.text is None:
            raise ValueError("markdown/jsonl source requires text")
        return self


class ModelFrictionClassRule(BaseModel):
    """One rule that names the class of a FRICTION row written with no class= cell and says who ends it (OMN-22563)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        min_length=1,
        description="Class name; {1} refers to the first group of the pattern.",
    )
    owner: str = Field(min_length=1, description="Who ends the class.")
    pattern: str = Field(
        min_length=1,
        description="Regular expression searched in the row's lane and text.",
    )


class ModelFrictionClassOwner(BaseModel):
    """Who ends a class that a writer already names but no ticket or lane owns (OMN-22563)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_name: str = Field(min_length=1)
    owner: str = Field(min_length=1)


class ModelFrictionRollupRequest(BaseModel):
    """All inputs are supplied by the caller; the node reads no files and has no clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sources: list[ModelFrictionRollupSource]
    since: str
    until: str
    generated_at: str
    bucket_hours: int = Field(gt=0)
    umbrella_tickets: list[str]
    aliases: list[str]
    class_rules: list[ModelFrictionClassRule] = Field(default_factory=list)
    class_owners: list[ModelFrictionClassOwner] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_parameters(self) -> Self:
        # Deferred: the rollup node package imports this module, so a top-level
        # import of its handlers would be circular.
        from omnimarket.nodes.node_friction_rollup_compute.handlers.friction_rollup_core import (
            _parse_aliases,
            parse_class_owners,
            parse_class_rules,
            parse_iso8601,
        )

        since = parse_iso8601(self.since)
        until = parse_iso8601(self.until)
        parse_iso8601(self.generated_at)
        if since >= until:
            raise ValueError("since must be earlier than until")
        _parse_aliases(self.aliases)
        parse_class_rules((r.name, r.owner, r.pattern) for r in self.class_rules)
        parse_class_owners((o.class_name, o.owner) for o in self.class_owners)
        return self
