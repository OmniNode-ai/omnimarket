# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of node_ci_summary_registry_compute (OMN-23505)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelCiSummaryRegistryRequest(BaseModel):
    """The parsed contents of the three registry files, or why one could not be read.

    The runtime reads the files; the handler never does. An ``*_error`` text means the
    file was missing or not JSON, and the matching parsed field is then ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sweep_exclusions: object = None
    sweep_exclusions_error: str | None = None
    check_modes_contract: object = None
    check_modes_overlay: object = None
    check_modes_error: str | None = None


class ModelCiSummaryExclusion(BaseModel):
    """A reasoned, ticketed, dated and expiring exception to the strict sweep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    ticket: str
    added: str
    expires: str


class ModelCiSummaryRegistry(BaseModel):
    """What the gate reads: the exclusions, each declared check's added conclusions, refusals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exclusions: dict[str, ModelCiSummaryExclusion]
    check_modes: dict[str, list[str]]
    refusals: list[str]
