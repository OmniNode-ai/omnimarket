# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports for reading and posting head check runs."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_review_gate_context_effect.models.model_review_context_post import (
    ModelHeadCheckRun,
)


class ReviewContextPortError(RuntimeError):
    """A port call failed; never carries a token."""


class ProtocolHeadCheckRunReader(Protocol):
    """Reads the head's check runs."""

    def read(
        self, *, repository: str, head_sha: str, check_name: str
    ) -> tuple[ModelHeadCheckRun, ...]:
        """The head's runs of the check name; a run of another head or name never counts."""
        ...


class ProtocolHeadCheckRunPoster(Protocol):
    """Posts a check run on the head."""

    def post(
        self,
        *,
        correlation_id: str,
        repository: str,
        head_sha: str,
        check_name: str,
        conclusion: str,
        title: str,
        summary: str,
    ) -> int:
        """Posts the run and returns the new check run id."""
        ...
