# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for the read-only Contract Graph IR GET surface."""

from __future__ import annotations

from omnibase_core.models.dashboard.model_component_contract import (
    ModelComponentContract,
)
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelContractGraphIrRequest"]


class ModelContractGraphIrRequest(BaseModel):
    """Request for deterministic Contract Graph IR import.

    ``discovery_roots`` are repo-relative directory roots to scan for backend
    ``contract.yaml`` files via manifest-driven discovery. At least one root
    is required. The importer excludes ``.venv``, ``omni_worktrees``, and
    generated surfaces automatically.

    ``repo_base_path`` is the absolute filesystem path of the repo root used
    to resolve ``discovery_roots`` to real filesystem paths at import time.
    It must be declared by the caller (no implicit env fallback).

    ``ui_components`` are UI component contracts (the Phase-0 UI primitive) to
    import via the ``ui_component`` dialect adapter alongside the discovered
    backend node contracts. UI component contracts are in-memory objects, not
    on-disk ``contract.yaml`` files, so they are passed explicitly rather than
    discovered. The single IR returned spans both dialects, with one hash
    manifest entry per source.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    discovery_roots: tuple[str, ...] = Field(
        ...,
        description="Repo-relative directory roots to scan for contract.yaml files",
        min_length=1,
    )
    repo_base_path: str = Field(
        ...,
        description="Absolute filesystem path of the repo root used to resolve discovery_roots",
        min_length=1,
    )
    ui_components: tuple[ModelComponentContract, ...] = Field(
        default=(),
        description="UI component contracts to import via the ui_component dialect adapter",
    )
