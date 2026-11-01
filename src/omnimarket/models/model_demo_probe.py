# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the demo readiness probes are given: a declared policy and a deployment overlay.

``ModelDemoProbePolicy`` is the ``probe_policy`` section of the node contract.
``ModelDemoProbeOverlay`` is the node's ``overlay.yaml`` (or the ``DEMO_DASHBOARD_URL``
setting): the dashboard the probes read is a deployment fact, so the package ships no
value for it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDemoProbePolicy(BaseModel):
    """The contract-declared bounds of one dashboard probe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: float = Field(
        gt=0, description="Seconds one probe request may take."
    )


class ModelDemoProbeOverlay(BaseModel):
    """The deployment's dashboard, supplied by whoever runs the probes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dashboard_url: str = Field(
        min_length=1,
        pattern=r"^https?://\S+$",
        description="Dashboard API base URL the probes read, without a trailing slash.",
    )


__all__ = ["ModelDemoProbeOverlay", "ModelDemoProbePolicy"]
