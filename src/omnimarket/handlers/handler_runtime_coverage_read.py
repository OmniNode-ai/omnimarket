# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared health-read composition: effect evidence followed by pure compute."""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.runtime_coverage import (
    ModelRuntimeCoverageRequest,
    ModelRuntimeCoverageResult,
)
from omnimarket.nodes.node_runtime_coverage_compute import HandlerRuntimeCoverageCompute
from omnimarket.nodes.node_runtime_coverage_effect import HandlerRuntimeCoverageEffect


async def read_runtime_coverage(
    request: ModelRuntimeCoverageRequest,
) -> ModelRuntimeCoverageResult:
    evidence = await HandlerRuntimeCoverageEffect().handle(request)
    return HandlerRuntimeCoverageCompute().handle(evidence)


def workspace_contract_roots(workspace: Path) -> tuple[str, ...]:
    """Canonical package roots only; never worktrees, fixtures or archived contracts."""
    roots = tuple(str(p) for p in sorted(workspace.glob("*/src/*/nodes")) if p.is_dir())
    if not roots:
        raise ValueError("workspace declares no node contract roots")
    return roots


async def read_health_runtime_coverage(
    workspace: Path, bus_lane: str, config_path: Path | None = None
) -> ModelRuntimeCoverageResult:
    """Read health policy from the diagnostics contract, never infer lane placement."""
    from importlib.resources import files

    import yaml

    policy = yaml.safe_load(
        files("omnimarket.nodes.node_platform_diagnostics")
        .joinpath("contract.yaml")
        .read_text()
    )["runtime_coverage"]
    from omnibase_infra.handlers import handler_workspace_config_root_resolve as root
    from omnibase_infra.models.workspace import ModelWorkspaceConfigRootRequest

    registry_path = (
        config_path
        or root.HandlerWorkspaceConfigRootResolve().require(
            ModelWorkspaceConfigRootRequest(workspace_root=workspace)
        )
        / policy["policy_relative_path"]
    )
    lane_policy = yaml.safe_load(registry_path.read_text())["bus_lanes"][bus_lane]
    return await read_runtime_coverage(
        ModelRuntimeCoverageRequest.model_validate(
            {
                "bus_lane": bus_lane,
                "runtime_lanes": lane_policy["runtime_lanes"],
                "contract_roots": workspace_contract_roots(workspace),
                "runtime_registries": lane_policy.get("runtime_registries", ()),
            }
        )
    )
