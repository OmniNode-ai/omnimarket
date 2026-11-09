# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the lab-fill eval catalog check (OMN-23178)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelEvalDecl(BaseModel):
    """One declared eval. `feeds` is untyped here on purpose: the handler judges it and names the bad entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    klass: str
    title: str
    brief: str
    inputs: tuple[str, ...] = ()
    runs_per_digest: int = 1
    feeds: str | None = None


class ModelEvalCatalogRequest(BaseModel):
    """The catalog to judge; `ruling` and `max_items` ride along from the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ruling: str = ""
    max_items: int = 1
    evals: tuple[ModelEvalDecl, ...] = ()


class ModelEvalFeedsDefect(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    eval_id: str
    feeds: str | None
    reason: str


class ModelEvalCatalogResult(BaseModel):
    """`ok` is true only when the catalog is non-empty and every eval's feeds is an allowed scenario."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    checked: int
    allowed: tuple[str, ...] = Field(default=())
    defects: tuple[ModelEvalFeedsDefect, ...] = ()
    summary: str = ""
