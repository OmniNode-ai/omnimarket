# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every host the temp-retention overlay declares ages out its pytest run folders (OMN-26097).

pytest keeps its run folders under ``/tmp/pytest-of-<user>``; the folder itself changes whenever a run
starts, so a rule on ``/tmp`` never reached an old ``pytest-N`` folder inside it. The overlay of the
node declares the root itself on each host, and the contract's leaf pattern lets it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    load_bounds,
    validate_declarations,
)
from omnimarket.nodes.node_temp_retention_effect.models.model_temp_retention import (
    ModelTempRetentionOverlay,
)

pytestmark = pytest.mark.unit

OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_temp_retention_effect/overlay.yaml"
)


def _overlay() -> ModelTempRetentionOverlay:
    return ModelTempRetentionOverlay.model_validate(
        yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    )


def test_the_overlay_validates_against_the_contract_bounds_for_every_host() -> None:
    bounds = load_bounds()
    for row in _overlay().hosts:
        validate_declarations(row, bounds)


def test_every_declared_host_has_a_pytest_temp_root_with_the_age_windows() -> None:
    for row in _overlay().hosts:
        pytest_roots = [
            root.path
            for root in row.temp_roots
            if Path(root.path).name.startswith("pytest-of-")
        ]
        assert pytest_roots, f"{row.name} declares no pytest temp root"
        assert row.windows.retain_hours > 0
        assert row.windows.pressure_retain_hours > 0


def test_the_hosts_the_ticket_names_for_temp_retention_are_declared() -> None:
    names = {row.name for row in _overlay().hosts}
    assert {"h051", "h053", "h201", "h202"} <= names
