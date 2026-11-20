# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory source adapters: the injected-break targets and the in-memory defaults."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from omnimarket.models.model_landing_review import ModelLandingReviewFacts
from omnimarket.models.model_review_gate_context import ModelReviewGatePrSubject
from omnimarket.nodes.node_review_gate_context_orchestrator.protocols.protocol_review_gate_sources import (
    ReviewGateSourceError,
)


class InMemoryOpenPullRequests:
    """The in-memory open pull requests; an optional error raises ReviewGateSourceError."""

    def __init__(
        self,
        subjects: tuple[ModelReviewGatePrSubject, ...] = (),
        *,
        error: str | None = None,
    ) -> None:
        self._subjects = subjects
        self._error = error

    def open_pull_requests(
        self, repositories: tuple[str, ...]
    ) -> tuple[ModelReviewGatePrSubject, ...]:
        if self._error is not None:
            raise ReviewGateSourceError(self._error)
        return self._subjects


class InMemoryReviewVerdicts:
    """The in-memory review verdicts; an optional error raises ReviewGateSourceError."""

    def __init__(
        self,
        facts: Mapping[str, ModelLandingReviewFacts] | None = None,
        *,
        error: str | None = None,
    ) -> None:
        self._facts: Mapping[str, ModelLandingReviewFacts] = (
            facts if facts is not None else {}
        )
        self._error = error

    def review_facts(self, now: datetime) -> Mapping[str, ModelLandingReviewFacts]:
        if self._error is not None:
            raise ReviewGateSourceError(self._error)
        return self._facts
