# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A second snapshot instance reads the overlay file its unit pins (OMN-23514).

The cutover repositories' unit sets ONEX_LANDING_SNAPSHOT_OVERLAY_FILE; the pinned file wins over the
node's overlay under ONEX_SKILL_OVERLAY_ROOTS, and a broken pinned file refuses naming the pin, never
falling back to the controller's overlay.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_pr_landing_snapshot_effect import runtime_snapshot
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_snapshot_overlay import (
    OVERLAY_FILE_ENV,
)

pytestmark = pytest.mark.unit


def _roots_overlay(tmp_path: Path) -> Path:
    node = tmp_path / "roots" / "node_pr_landing_snapshot_effect"
    node.mkdir(parents=True)
    (node / "overlay.yaml").write_text(
        (
            Path(runtime_snapshot.__file__).resolve().parent
            / "snapshot.overlay.example.yaml"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return tmp_path / "roots"


def test_a_broken_pinned_file_refuses_naming_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(_roots_overlay(tmp_path)))
    pinned = tmp_path / "cutover.yaml"
    pinned.write_text("backend: not-a-backend\n", encoding="utf-8")
    monkeypatch.setenv(OVERLAY_FILE_ENV, str(pinned))
    assert runtime_snapshot.main([]) == 3
    line = json.loads(capsys.readouterr().out)
    assert line["status"] == "overlay_refused"
    assert OVERLAY_FILE_ENV in line["detail"]


def test_control_without_the_pin_the_roots_overlay_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(_roots_overlay(tmp_path)))
    monkeypatch.delenv(OVERLAY_FILE_ENV, raising=False)
    runtime_snapshot.main([])
    line = json.loads(capsys.readouterr().out)
    assert OVERLAY_FILE_ENV not in str(line.get("detail", ""))
