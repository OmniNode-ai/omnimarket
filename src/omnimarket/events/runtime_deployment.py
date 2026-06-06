# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared runtime deployment event surfaces."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID


class RuntimeLaneLike(Protocol):
    """Runtime lane enum shape shared across deployment-proof consumers."""

    @property
    def value(self) -> str:
        """Wire value for the runtime lane."""


class ModelRuntimeDeploymentProof(Protocol):
    """Shared deployment-proof shape consumed outside the redeploy node."""

    correlation_id: UUID
    deployment_id: UUID
    runtime_lane: RuntimeLaneLike
    source_sha: str
    image_digest: str
    compose_project: str
    health_status: str
    ready_status: str
    probed_at: datetime
    status: str
    promotion_batch_id: str | None
    runtime_addresses: Sequence[str]
    topology_manifest_sha256: str | None
    package_versions: Mapping[str, str]
    runtime_source_hash: str | None
    consumer_groups: Sequence[str]
    runtime_sweep_input_ref: str | None


__all__ = [
    "ModelRuntimeDeploymentProof",
    "RuntimeLaneLike",
]
