# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the host role baseline tests: the example overlay as the baselines and a request."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_host_role import EnumHostRole
from omnimarket.nodes.node_host_role_baseline_compute.models import (
    ModelHostRoleBaselineRequest,
    ModelHostRoleBaselines,
)

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_role_baseline_compute"
)


def example_document() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(
        (NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    return document


def baselines() -> ModelHostRoleBaselines:
    return ModelHostRoleBaselines.model_validate(example_document())


def request(
    host: str,
    role: EnumHostRole | None,
    *,
    registry_root: str = "/srv/registry",
    changed: ModelHostRoleBaselines | None = None,
) -> ModelHostRoleBaselineRequest:
    return ModelHostRoleBaselineRequest(
        host=host,
        host_role=role,
        registry_root=registry_root,
        ssh_target=None,
        baselines=changed or baselines(),
    )
