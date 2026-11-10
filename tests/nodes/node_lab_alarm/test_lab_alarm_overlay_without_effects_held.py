# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker lag read keeps reporting when the overlay has no effects_held block (OMN-23304).

On 2026-11-09 the effects-held condition (OMN-23106) made ``effects_held`` a required overlay key.
The deployed overlay had no such block, so every scheduled broker-lag run exited 1 with
``LAB-ALARM-REFUSED ... fields ['effects_held']`` and the floor row lab:broker_lag went stale.

Golden: an overlay without the block loads, the broker-lag verb runs to a typed outcome instead of a refusal.
Error: the effects-held verbs refuse a missing block with their own typed message and exit 1, and
the control (the same overlay with the block removed from the model) fails the load.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay, load_overlay
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import main
from tests.nodes.node_lab_alarm.test_lab_alarm_chain_events import _NODE

pytestmark = pytest.mark.unit


def _document_without_effects_held() -> dict[str, object]:
    document = yaml.safe_load(
        (_NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    del document["effects_held"]
    return document


def _install_overlay(root: Path, document: dict[str, object]) -> dict[str, str]:
    node_dir = root / "node_lab_alarm"
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def test_golden_an_overlay_without_effects_held_loads(tmp_path: Path) -> None:
    environ = _install_overlay(tmp_path, _document_without_effects_held())

    overlay = load_overlay(environ)

    assert overlay.effects_held is None
    assert overlay.lag.record_condition


def test_golden_the_broker_lag_verb_runs_and_reports_without_effects_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environ = _install_overlay(tmp_path, _document_without_effects_held())
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", environ["ONEX_SKILL_OVERLAY_ROOTS"])

    code = main(["broker-lag"])

    out = capsys.readouterr()
    assert "LAB-ALARM-REFUSED" not in out.err
    # The in-memory manifest carries no contract, so the read is typed INDETERMINATE (exit 3), never a refusal (exit 1).
    assert code == 3


def test_error_the_effects_held_verbs_refuse_a_missing_block_by_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environ = _install_overlay(tmp_path, _document_without_effects_held())
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", environ["ONEX_SKILL_OVERLAY_ROOTS"])

    for verb in ("effects-held", "effects-held-probe"):
        assert main([verb]) == 1
        assert "LAB-ALARM-REFUSED effects_held" in capsys.readouterr().err


def test_control_every_other_key_is_still_required() -> None:
    document = _document_without_effects_held()
    del document["lag"]

    with pytest.raises(ValueError, match="lag"):
        ModelLabAlarmOverlay.model_validate(document)
