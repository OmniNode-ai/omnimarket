# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line refuses an undeclared key with a typed error and a non-zero exit, and a bad value file the same way."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.overlay_store import ModelOverlayStoreLoadOverlay
from omnimarket.nodes.node_overlay_setting_set_effect import runtime_overlay_setting_set
from tests.unit.nodes.node_overlay_setting_set.support import ACTOR, KEY, OVERLAY_ID

pytestmark = pytest.mark.unit


def _inmemory_loader_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loader's overlay the command reads its bus from: the in-memory bus, so no broker is touched."""
    monkeypatch.setattr(
        runtime_overlay_setting_set,
        "load_structured_overlay",
        lambda *_args, **_kwargs: ModelOverlayStoreLoadOverlay(
            bus="inmemory", loaded_by="test"
        ),
    )


def test_an_undeclared_key_exits_65_and_names_the_declared_keys(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _inmemory_loader_overlay(monkeypatch)
    code = runtime_overlay_setting_set.main(
        [
            "--overlay",
            OVERLAY_ID,
            "--key",
            "lanes.lane-a.enabled",
            "--value-json",
            "false",
            "--actor",
            ACTOR,
            "--read-window-s",
            "0.05",
        ]
    )
    err = capsys.readouterr().err
    assert code == 65
    assert "SettingNotDeclaredError" in err
    assert "lanes.{lane}.lazy_start" in err


def test_the_declared_key_reaches_the_store_read_positive_control(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same command with the declared key gets past the declaration and stops at the empty store, a different error."""
    _inmemory_loader_overlay(monkeypatch)
    code = runtime_overlay_setting_set.main(
        [
            "--overlay",
            OVERLAY_ID,
            "--key",
            KEY,
            "--value-json",
            "{}",
            "--actor",
            ACTOR,
            "--read-window-s",
            "0.05",
        ]
    )
    err = capsys.readouterr().err
    assert code == 65
    assert "OverlayNotInStoreError" in err


def test_a_blank_actor_exits_65(capsys: pytest.CaptureFixture[str]) -> None:
    code = runtime_overlay_setting_set.main(
        [
            "--overlay",
            OVERLAY_ID,
            "--key",
            KEY,
            "--value-json",
            "{}",
            "--actor",
            "  ",
        ]
    )
    assert code == 65
    assert "actor" in capsys.readouterr().err


def test_a_value_file_that_is_not_json_exits_65(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "value.json"
    bad.write_text("{not json", encoding="utf-8")
    code = runtime_overlay_setting_set.main(
        [
            "--overlay",
            OVERLAY_ID,
            "--key",
            KEY,
            "--value-file",
            str(bad),
            "--actor",
            ACTOR,
        ]
    )
    assert code == 65
    assert "JSONDecodeError" in capsys.readouterr().err
