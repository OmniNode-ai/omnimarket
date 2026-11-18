# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders and fakes shared by the branch deploy probe effect tests (OMN-24027)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from omnimarket.models.model_dev_deploy_reading import ModelRuntimeProbeFact
from omnimarket.nodes.node_branch_deploy_probe_effect.models.model_branch_deploy_probe import (
    ModelBranchDeployProbeRequest,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.overlay import (
    ModelBranchDeployProbeOverlay,
)

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_branch_deploy_probe_effect"
)
RUN_ID = uuid4()
HEAD = "a" * 40
PROBED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def overlay() -> ModelBranchDeployProbeOverlay:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    return ModelBranchDeployProbeOverlay.model_validate(document)


def request(
    phase: str = "deploy",
    **cells: Any,
) -> ModelBranchDeployProbeRequest:
    return ModelBranchDeployProbeRequest(
        correlation_id=cells.pop("correlation_id", RUN_ID),
        phase=phase,
        expected_sha=cells.pop("expected_sha", HEAD),
        **cells,
    )


def fact(
    observed: str | None,
    ok: bool,
    expected_sha: str = HEAD,
    probed_at: datetime = PROBED_AT,
) -> ModelRuntimeProbeFact:
    return ModelRuntimeProbeFact(
        probe_id="probe-test",
        probed_at=probed_at,
        expected_sha=expected_sha,
        observed_sha=observed,
        ok=ok,
    )
