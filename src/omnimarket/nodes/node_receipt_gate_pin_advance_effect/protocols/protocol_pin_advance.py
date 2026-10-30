# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports the pin advance effect talks through (OMN-20134).

Each port is blocking; the handler runs them off the event loop. A port that
cannot answer raises :class:`PinAdvanceError`, which the runtime dead-letters
and ends in the failure terminal: a half-read caller table must never produce a
partial advance.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from omnimarket.nodes.node_receipt_gate_pin_advance_compute.models import (
    ModelCallerPlan,
    ModelCallerSnapshot,
    ModelCoreDevTag,
    ModelFileEdit,
    ModelPinCaller,
)


class PinAdvanceError(RuntimeError):
    """A port could not read or write what the advance needs."""


class ProtocolCoreTagSource(Protocol):
    """The source repository's dev-merge tags, each with its gate file's blob id."""

    def dev_tags(self, *, required_tag: str) -> tuple[ModelCoreDevTag, ...]:
        """Every dev tag, refusing (PinAdvanceError) when ``required_tag`` is absent."""
        ...


class ProtocolCallerRepository(Protocol):
    """Read a caller's pin-site files at its base head; commit edits on a branch."""

    def read_snapshot(self, caller: ModelPinCaller) -> ModelCallerSnapshot:
        """The pin-site files that exist at the base branch head; a missing file is absent."""
        ...

    def publish_branch(
        self,
        *,
        repository: str,
        base_head_sha: str,
        branch: str,
        message: str,
        edits: tuple[ModelFileEdit, ...],
    ) -> str:
        """One commit holding every edit, on ``branch``; returns the branch head.

        A branch that already exists is not touched: its head is returned, so a
        retried trigger finds its earlier work.
        """
        ...


class ProtocolPinPrOpener(Protocol):
    """Open the pull request for a published branch through the write node."""

    def open(
        self,
        plan: ModelCallerPlan,
        *,
        branch_head_sha: str,
        requested_by_lane: str,
        correlation_id: UUID,
    ) -> tuple[bool, int | None, str | None]:
        """(replayed, pull request number, url); raises PinAdvanceError on refusal."""
        ...
