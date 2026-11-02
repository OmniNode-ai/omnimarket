# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reads behind the merged-and-running count, so a test or a deployment binds its own sources."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.model_merged_running_count import (
    ModelMrcEvidence,
    ModelMrcEvidenceRead,
    ModelMrcHost,
    ModelMrcLiveLane,
    ModelMrcPr,
)


@runtime_checkable
class ProtocolMergedRunningSources(Protocol):
    """Merges and other pull request states, adoption evidence, deployed evidence (the dev lane's lab-pass receipt), the declared host table with its placement read, live lanes."""

    def read_prs(self) -> tuple[ModelMrcPr, ...]: ...

    def read_evidence(self) -> tuple[ModelMrcEvidence, ...]: ...

    def read_deployed_evidence(self) -> ModelMrcEvidenceRead: ...

    def read_hosts(self) -> tuple[ModelMrcHost, ...]: ...

    def read_live_lanes(self) -> tuple[ModelMrcLiveLane, ...]: ...


__all__: list[str] = ["ProtocolMergedRunningSources"]
