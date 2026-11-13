# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installer's tick form of the hourly reading: a probe that writes nothing, and a pass that is the daily verb (OMN-23619)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnimarket.nodes.node_pilot_measurement_effect import (
    runtime_pilot_measurement_tick as tick,
)

pytestmark = pytest.mark.unit

_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_pilot_measurement_effect/overlay.example.yaml"
)


def test_probe_loads_the_overlay_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    node_dir = tmp_path / "overlays" / "node_pilot_measurement_effect"
    node_dir.mkdir(parents=True)
    shutil.copy(_EXAMPLE, node_dir / "overlay.yaml")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    before = sorted(p.name for p in tmp_path.rglob("*"))
    assert tick.main(["--probe", "--omni-home", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "PROBE ok\n"
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_probe_without_an_overlay_is_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Failing control: a unit whose overlay cannot be read must not pass its start check."""
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty"))
    assert tick.main(["--probe"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_a_pass_is_the_daily_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(
        tick, "measurement_main", lambda argv: seen.append(list(argv)) or 7
    )
    assert tick.main(["--omni-home", "/x", "--state-dir", "/y"]) == 7
    assert seen == [["daily"]]
