# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared health-read composition: effect evidence followed by pure compute."""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.runtime_coverage import (
    ModelRuntimeCoverageRequest,
    ModelRuntimeCoverageResult,
    ModelRuntimeRegistryEndpoint,
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
    from omnibase_infra.runtime.service_kernel import workspace_runtime_config_root

    registry_path = (
        config_path
        or workspace_runtime_config_root(workspace) / policy["policy_relative_path"]
    )
    document = yaml.safe_load(registry_path.read_text())
    lane_policy = document["bus_lanes"][bus_lane]
    runtime_lanes = tuple(lane_policy["runtime_lanes"])
    endpoints = tuple(
        ModelRuntimeRegistryEndpoint.model_validate(row)
        for row in lane_policy.get("runtime_registries", ())
    )
    return await read_runtime_coverage(
        ModelRuntimeCoverageRequest(
            bus_lane=bus_lane,
            runtime_lanes=runtime_lanes,
            contract_roots=workspace_contract_roots(workspace),
            runtime_registries=endpoints,
        )
    )
