# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick reads its class rules from the overlay home; these tests stand a file of the rollup node's shipped rules in for it."""

from __future__ import annotations

from pathlib import Path

import pytest

import omnimarket.nodes.node_friction_rollup_compute as compute_package

CLASS_OVERLAY_ENV = "ONEX_FRICTION_CLASS_OVERLAY"
OVERLAY_HOME_FILE = Path(compute_package.__file__).parent / "overlay.example.yaml"


@pytest.fixture(autouse=True)
def overlay_home_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CLASS_OVERLAY_ENV, str(OVERLAY_HOME_FILE))
