# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the provision overlay resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.topic_provision.enum_topic_provision import (
    EnumTopicProvisionMode,
)
from omnimarket.models.topic_provision.model_topic_provision_overlay import (
    ModelTopicProvisionOverlay,
)
from omnimarket.nodes.node_topic_provision_plan_compute.overlay import (
    NODE_NAME,
    load_provision_overlay,
    load_provision_rule,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_topic_provision_plan_compute"
    / "contract.yaml"
)


def _expected_contract_mode() -> EnumTopicProvisionMode:
    return EnumTopicProvisionMode(
        yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["config"]["rule"]["mode"]
    )


def _overlay_root(tmp_path: Path, document: str) -> dict[str, str]:
    root = tmp_path / NODE_NAME
    root.mkdir(parents=True, exist_ok=True)
    (root / "overlay.yaml").write_text(document, encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": os.pathsep.join([str(tmp_path)])}


def test_no_overlay_returns_contract_config_mode(tmp_path: Path) -> None:
    rule = load_provision_rule(environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert rule.mode == _expected_contract_mode()


def test_overlay_mode_overrides_contract(tmp_path: Path) -> None:
    environ = _overlay_root(tmp_path, "mode: profile\n")
    rule = load_provision_rule(environ=environ)
    assert rule.mode == EnumTopicProvisionMode.PROFILE


def test_overlay_unknown_key_refuses(tmp_path: Path) -> None:
    environ = _overlay_root(tmp_path, "mode: profile\nnot_a_key: true\n")
    with pytest.raises(NodeOverlayError):
        load_provision_rule(environ=environ)


def test_load_provision_overlay_reads_fields(tmp_path: Path) -> None:
    environ = _overlay_root(
        tmp_path,
        "mode: profile\nprofile: main\nlane_manifest:\n  - delegation\ncontract_roots:\n  - /contracts\n",
    )
    overlay = load_provision_overlay(environ=environ)
    assert overlay.mode == EnumTopicProvisionMode.PROFILE
    assert overlay.profile == "main"
    assert overlay.lane_manifest == ("delegation",)
    assert overlay.contract_roots == ("/contracts",)


def test_no_overlay_returns_all_default_overlay(tmp_path: Path) -> None:
    overlay = load_provision_overlay(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    )
    assert overlay == ModelTopicProvisionOverlay()
