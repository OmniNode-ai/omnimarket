# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26651: the shipped lab daemon overlay names no model for the fix lane.

The model a detached fix lane runs on is a declared setting
(ONEX_LAB_FIX_LANE_MODEL) the overlay expands from the tick's environment, like every
other host fact, and an unset setting refuses the overlay instead of running a default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_daemon_sync_effect.__main__ import (
    DEFAULT_OVERLAY,
    load_overlay,
)

_SETTING = "ONEX_LAB_FIX_LANE_MODEL"
_FIXTURE_MODEL = "fixture-fix-lane-model"


def _value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_fix_lane_model_is_read_from_the_declared_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_SETTING, _FIXTURE_MODEL)
    monkeypatch.setenv("OMNI_PLUGIN_ROOT", "/plugin")
    overlay = load_overlay(Path(DEFAULT_OVERLAY), [])
    assert _value_after(list(overlay.fix_lane), "--model") == _FIXTURE_MODEL


def test_fix_lane_with_the_setting_unset_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_SETTING, raising=False)
    monkeypatch.setenv("OMNI_PLUGIN_ROOT", "/plugin")
    with pytest.raises(ValueError, match=_SETTING):
        load_overlay(Path(DEFAULT_OVERLAY), [])
