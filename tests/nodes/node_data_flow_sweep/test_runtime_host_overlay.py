# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime host of a remote lane comes from the deployment, never the package."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_data_flow_sweep.lane_target import (
    KNOWN_LANES,
    LaneResolutionError,
    resolve_lane_target,
)

NODE = "node_data_flow_sweep"
OVERLAY_HOST = "overlay-host.example.invalid"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_DATA_FLOW_RUNTIME_HOST", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)


def _overlay_root(tmp_path: Path, content: object) -> Path:
    path = tmp_path / "overlays" / NODE / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(content), encoding="utf-8")
    return tmp_path / "overlays"


def test_overlay_supplies_the_runtime_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        str(_overlay_root(tmp_path, {"runtime_host": OVERLAY_HOST})),
    )
    target = resolve_lane_target("dev")
    assert target.runtime_host == OVERLAY_HOST
    assert target.is_remote is True


def test_environment_beats_the_overlay_and_the_kwarg_beats_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        str(_overlay_root(tmp_path, {"runtime_host": OVERLAY_HOST})),
    )
    monkeypatch.setenv("ONEX_DATA_FLOW_RUNTIME_HOST", "env-host.example.invalid")
    assert resolve_lane_target("dev").runtime_host == "env-host.example.invalid"
    assert (
        resolve_lane_target(
            "dev", runtime_host="kwarg-host.example.invalid"
        ).runtime_host
        == "kwarg-host.example.invalid"
    )


@pytest.mark.parametrize("lane", KNOWN_LANES)
def test_remote_lane_without_any_host_refuses_and_names_how_to_supply_one(
    lane: str,
) -> None:
    with pytest.raises(LaneResolutionError) as caught:
        resolve_lane_target(lane)
    message = str(caught.value)
    assert "ONEX_DATA_FLOW_RUNTIME_HOST" in message
    assert "ONEX_SKILL_OVERLAY_ROOTS" in message
    assert f"{NODE}/overlay.yaml" in message


def test_local_lane_needs_no_overlay() -> None:
    target = resolve_lane_target("local")
    assert target.runtime_host == ""
    assert target.is_remote is False


def test_overlay_with_an_unknown_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        str(_overlay_root(tmp_path, {"runtime_hots": OVERLAY_HOST})),
    )
    with pytest.raises(LaneResolutionError, match="runtime_hots"):
        resolve_lane_target("dev")


def test_overlay_with_a_non_string_host_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        str(_overlay_root(tmp_path, {"runtime_host": ["a", "b"]})),
    )
    with pytest.raises(LaneResolutionError, match="runtime_host must be a string"):
        resolve_lane_target("dev")
