# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the overlay environment probe: rows for the host's nodes as names and states, never a value (OMN-26549)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_installer_overlay_env_probe import (
    runtime_overlay_env_probe_tick as tick,
)

SET_NAME = "FIXTURE_TICK_SET_NAME"
UNSET_NAME = "FIXTURE_TICK_UNSET_NAME"
SENTINEL = "tick-value-that-must-never-appear"
NODE = "node_fixture_tick_effect"


def _overlays(tmp_path: Path) -> Path:
    root = tmp_path / "overlays"
    (root / NODE).mkdir(parents=True)
    (root / NODE / "overlay.yaml").write_text(
        yaml.safe_dump({"a": {"host_env": SET_NAME}, "b": {"token_env": UNSET_NAME}})
    )
    (root / "not_a_node").mkdir()
    return root


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = _overlays(tmp_path)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    monkeypatch.setenv("LAB_WORK_HOST_NAME", "h-fixture")
    monkeypatch.setenv(SET_NAME, SENTINEL)
    monkeypatch.delenv(UNSET_NAME, raising=False)
    return tmp_path / "state"


def test_the_request_names_the_host_the_overlay_nodes_and_the_set_names(
    environment: Path,
) -> None:
    request = tick.build_request(os.environ)
    (host,) = request.hosts
    assert host.host_name == "h-fixture"
    assert host.nodes == (NODE,)
    assert SET_NAME in host.set_names
    assert UNSET_NAME not in host.set_names
    assert SENTINEL not in request.model_dump_json()


def test_a_tick_prints_one_line_per_name_and_keeps_no_value(
    environment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert tick.main(["--omni-home", "/x", "--state-dir", str(environment)]) == 0
    out = capsys.readouterr().out
    assert f"PROBE-ROW host=h-fixture node={NODE} name={SET_NAME} state=set" in out
    assert f"PROBE-ROW host=h-fixture node={NODE} name={UNSET_NAME} state=unset" in out
    assert f"PROBE-UNAVAILABLE host=h-fixture node={NODE} unset={UNSET_NAME}" in out
    assert "PROBE-SUMMARY status=success rows=2 unset=1 unavailable=1" in out
    record = (environment / "last-run.json").read_text()
    assert json.loads(record)["exit_code"] == 0
    assert SENTINEL not in out
    assert SENTINEL not in record


def test_a_tick_with_no_host_is_indeterminate(
    environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("LAB_WORK_HOST_NAME")
    assert tick.main(["--omni-home", "/x", "--state-dir", str(environment)]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
    assert json.loads((environment / "last-run.json").read_text())["exit_code"] == 3
