# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocols of the lane-brief context pack: a deterministic tier reader and one rung of the delegation chain."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from omnimarket.enums.enum_lane_brief_tier import EnumLaneBriefRung
from omnimarket.models.context_pack_orchestrator.model_lane_brief_context import (
    ModelLaneBriefContextRequest,
    ModelLaneBriefExemplarCandidate,
    ModelLaneBriefExemplarChoice,
    ModelLaneBriefSummary,
)


@runtime_checkable
class ProtocolLaneBriefTierReader(Protocol):
    """Reads one deterministic tier by code (a graph query node, the repository); it never calls a model."""

    def read(self, request: ModelLaneBriefContextRequest) -> str:
        """The tier's text."""
        ...


@runtime_checkable
class ProtocolLaneBriefRung(Protocol):
    """A delegate for exemplar selection and the lane-history summary."""

    name: EnumLaneBriefRung

    def select(
        self,
        request: ModelLaneBriefContextRequest,
        candidates: Sequence[ModelLaneBriefExemplarCandidate],
    ) -> ModelLaneBriefExemplarChoice:
        """Choose the closest candidate and cut its shape excerpt; raise LaneBriefRungError to fall back."""
        ...

    def summarize(
        self, request: ModelLaneBriefContextRequest, facts: str
    ) -> ModelLaneBriefSummary:
        """Summarize the supplied ledger references."""
        ...
