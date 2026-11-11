# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from pathlib import Path

import pytest
import yaml

from scripts import ci_bus_lanes


def test_private_overlay_selected(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(tmp_path))
    import hashlib

    data = b"lanes: {example: {broker: broker.example.invalid:9092, security_protocol: PLAINTEXT}}"
    digest = hashlib.sha256(data).hexdigest()
    contract = tmp_path / "contract.yaml"
    contract.write_text(f"overlay_sha256: {digest}\n")
    monkeypatch.setattr(ci_bus_lanes, "LANE_OVERLAY_PATH", contract)
    path = tmp_path / f"config/onex/overlays/ci_bus_lanes/{digest}.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "lanes: {example: {broker: broker.example.invalid:9092, security_protocol: PLAINTEXT}}"
    )
    assert (
        ci_bus_lanes.load_lane_overlay()["lanes"]["example"]["broker"]
        == "broker.example.invalid:9092"
    )


def test_missing_overlay_fails_named(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(tmp_path))
    with pytest.raises(
        ci_bus_lanes.LaneOverlayError, match="ONEX_WORKSPACE_CONFIG_ROOT"
    ):
        ci_bus_lanes.load_lane_overlay()


def test_public_default_is_empty():
    root = Path(__file__).resolve().parents[3]
    raw = yaml.safe_load((root / "config/ci_bus_lanes.yaml").read_text())
    assert len(raw.pop("overlay_sha256")) == 64
    assert raw == {"default": "inmemory", "lanes": {"prod": {"broker": "inmemory"}}}
