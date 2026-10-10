# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validated caller-provided sources and the three rollup output representations."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_friction_rollup_compute.handlers.friction_rollup_core import (
    _parse_aliases,
    parse_iso8601,
)


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

    @model_validator(mode="after")
    def validate_parameters(self) -> Self:
        since = parse_iso8601(self.since)
        until = parse_iso8601(self.until)
        parse_iso8601(self.generated_at)
        if since >= until:
            raise ValueError("since must be earlier than until")
        _parse_aliases(self.aliases)
        return self


class ModelFrictionRollupResult(BaseModel):
    """Exactly the old report, markdown rendering and indented JSON text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report: dict[str, Any]
    markdown: str
    json_text: str
