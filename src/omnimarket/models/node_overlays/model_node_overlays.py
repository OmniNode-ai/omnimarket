# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the small node overlays the overlay home holds (OMN-22478).

Each is what the overlay home validates one node's ``overlay.yaml`` through, and imports pydantic
alone, because that check installs pydantic and pyyaml and nothing else. The nodes read the same
fields through ``load_node_overlay``; the key sets here are the ones those readers name. No value
lives here: the documents are private deployment data and stay in the overlay home.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _ModelOverlayDocument(BaseModel):
    """Closed to stray keys and frozen: a typo in an overlay refuses instead of passing unread."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelLabSliPackOverlay(_ModelOverlayDocument):
    """``lab_sli_pack``: whether the deployment enables the lab SLI pack."""

    status: Literal["enabled", "disabled"]


class ModelRuntimeHostOverlay(_ModelOverlayDocument):
    """``node_data_flow_sweep`` and ``node_database_sweep``: the host their runtime is reached on."""

    runtime_host: NonBlank


class ModelIntegrationSweepOverlay(_ModelOverlayDocument):
    """``node_integration_sweep_orchestrator``: where the runtime and its surfaces are probed."""

    runtime_host: NonBlank
    runtime_repo_path: NonBlank
    stability_test_runtime_url: NonBlank
    container_health_host: NonBlank
    infra_runtime_host: NonBlank
    projection_api_url: NonBlank


class ModelPrStateReadOverlay(_ModelOverlayDocument):
    """``node_pr_state_read_effect``: the projection API the pull request state is read from (OMN-23038)."""

    projection_api_url: NonBlank


class ModelLabDiskHygieneOverlay(_ModelOverlayDocument):
    """``node_lab_disk_hygiene_effect``: the lane census the sweep reads, relative to the registry root."""

    census_path: NonBlank


class ModelDuplicateGuardGeneratedPathsOverlay(_ModelOverlayDocument):
    """``node_pr_duplicate_guard_compute``: per repository, the paths every pull request of a kind regenerates."""

    generated_paths: dict[NonBlank, tuple[NonBlank, ...]] = Field(min_length=1)


class ModelRouteCheckOverlay(_ModelOverlayDocument):
    """``node_delegation_rubric_check_compute``: the served model id of the local rung (OMN-19184)."""

    route_check_local_model: NonBlank


class ModelAbCompareRegistryEntry(_ModelOverlayDocument):
    """One model of the A/B compare registry; ``model_id_default`` may be empty, which skips the model."""

    id: NonBlank
    display_name: NonBlank
    protocol: NonBlank
    location: NonBlank
    cost_per_1k_input: float = Field(ge=0)
    cost_per_1k_output: float = Field(ge=0)
    context_window: int = Field(gt=0)
    endpoint_env: str | None = None
    endpoint: str | None = None
    model_id_env: str | None = None
    model_id_default: str | None = None
    model_id: str | None = None
    requires_key: str | None = None


class ModelAbCompareRegistryOverlay(_ModelOverlayDocument):
    """``node_ab_compare_orchestrator``: the whole registry of models the A/B compare runs (OMN-19184)."""

    schema_version: NonBlank
    models: tuple[ModelAbCompareRegistryEntry, ...] = Field(min_length=1)


class ModelPytestHostGuardOverlay(_ModelOverlayDocument):
    """``node_pytest_full_suite_host_guard``: per host-table row label the ``PREPUSH_*`` variable that
    replaces that row's hostname (OMN-19184)."""

    legacy_override_by_label: dict[NonBlank, NonBlank]


__all__ = [
    "ModelAbCompareRegistryEntry",
    "ModelAbCompareRegistryOverlay",
    "ModelDuplicateGuardGeneratedPathsOverlay",
    "ModelIntegrationSweepOverlay",
    "ModelLabDiskHygieneOverlay",
    "ModelLabSliPackOverlay",
    "ModelPytestHostGuardOverlay",
    "ModelRouteCheckOverlay",
    "ModelRuntimeHostOverlay",
]
