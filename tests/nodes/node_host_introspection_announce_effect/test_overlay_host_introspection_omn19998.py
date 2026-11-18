# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``host_introspection`` overlay loads from its example and refuses a missing value by name (OMN-19998)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.host_introspection_overlay import (
    load_host_introspection_overlay,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from tests.host_introspection_support import EXAMPLE_OVERLAY, install_overlay

pytestmark = pytest.mark.unit


def test_golden_the_example_loads_and_declares_the_values_code_must_not_hold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    assert overlay.cadence_seconds >= 1
    assert {r.name for r in overlay.runtimes} >= {"python", "docker"}
    assert overlay.lane_accounts[0].tool in overlay.capacity_tools


@pytest.mark.parametrize(
    "key",
    [
        "cadence_seconds",
        "probe_timeout_seconds",
        "model_endpoint_template",
        "capacity_tools",
    ],
)
def test_error_a_missing_value_refuses_naming_the_key_and_nothing_falls_back(
    key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    del document[key]
    target = tmp_path / "roots" / "host_introspection" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "roots"))
    with pytest.raises(NodeOverlayError, match=key):
        load_host_introspection_overlay()


def test_error_no_overlay_root_refuses_naming_the_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty"))
    with pytest.raises(NodeOverlayError, match="host_introspection"):
        load_host_introspection_overlay()
