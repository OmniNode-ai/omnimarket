# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for the verification sweep orchestrator."""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

VerificationCheckType = Literal["dashboard", "database", "dod_evidence"]


class ModelVerificationSweepOrchestratorRequest(BaseModel):
    """Typed start command for the verification sweep orchestrator.

    ``correlation_id`` defaults when absent so the typed command validates
    against the runtime-injected envelope ``correlation_id`` on the canonical
    ``onex skill verification_sweep`` / ``onex run-node`` dispatch path
    (mirrors ``ModelIntegrationSweepOrchestratorRequest``, OMN-13145). Without
    this field the ``extra="forbid"`` model raised ``extra_forbidden`` when the
    local runtime built it from the envelope payload dict that carries
    ``correlation_id`` — rejecting the request BEFORE ``handle()`` ran and
    making the sweep undispatchable (OMN-14552).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        default_factory=uuid4,
        description="Sweep run correlation ID (envelope-injected on dispatch).",
    )
    targets: tuple[str, ...] = Field(
        default=(),
        description="Ticket IDs to verify (e.g. ['OMN-5400', 'OMN-5401']).",
    )
    epic: str | None = Field(
        default=None,
        description="Epic ID — discover and verify all child tickets.",
    )
    check_types: tuple[VerificationCheckType, ...] = Field(
        default=(),
        description=(
            "Verification phases to run: dashboard | database | dod_evidence. "
            "Empty = all phases."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="When true, print results without writing receipts or Linear comments.",
    )
    pr: str | None = Field(
        default=None,
        description="GitHub PR reference (owner/repo#number) for pre-merge mode.",
    )
    timeout_seconds: int = Field(
        default=30,
        description="Hard timeout (seconds) for a single pre-merge PR verification run.",
    )
