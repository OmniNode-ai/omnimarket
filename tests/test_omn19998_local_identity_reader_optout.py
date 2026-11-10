# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local-identity bus exposure declares a reasoned, self-expiring reader opt-out."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_local_identity/contract.yaml"
)


def test_bus_backed_exposure_declares_a_reasoned_reader_opt_out() -> None:
    """exposure-reader-coverage refuses a bus_backed exposure with no reader."""
    projection_api = yaml.safe_load(CONTRACT.read_text())["projection_api"]
    assert projection_api["bus_backed"] is True
    assert projection_api["consumers"] == "none"
    reason = projection_api["consumers_reason"]
    assert isinstance(reason, str)
    assert reason.strip()
    assert "onex.snapshot.projection.local-identity.v1" in reason
