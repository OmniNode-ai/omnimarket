# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders shared by the host install readback compute tests (OMN-23957)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import yaml

from omnimarket.nodes.node_host_install_readback_compute.models import (
    ModelHostInstallReadbackRequest,
)
from omnimarket.nodes.node_host_install_readback_compute.overlay import (
    ModelHostInstallReadbackOverlay,
)

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_install_readback_compute"
)
HOST = "host-a"
UNIT = "unit-a"
RUN_ID = uuid4()


def overlay() -> ModelHostInstallReadbackOverlay:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    return ModelHostInstallReadbackOverlay.model_validate(document)


def request(
    *,
    correlation_id: UUID = RUN_ID,
    host: str = HOST,
    unit: str = UNIT,
    render_text: str = "rendered unit",
    installed_present: bool = True,
    installed_text: str = "rendered unit",
) -> ModelHostInstallReadbackRequest:
    return ModelHostInstallReadbackRequest(
        correlation_id=correlation_id,
        host=host,
        unit=unit,
        render_text=render_text,
        installed_present=installed_present,
        installed_text=installed_text,
    )
