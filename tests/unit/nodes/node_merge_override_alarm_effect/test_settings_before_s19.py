# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing App login is required only where S19 is in force."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    EnumMergeOverrideVerdict,
    ModelMergeOverrideResult,
    ModelMergeOverrideSettings,
    ModelMergeOverrideVerdictEvent,
)
from tests.unit.nodes.node_merge_override_alarm_effect.support import (
    REPO,
    arm_receipt,
    drive_merge,
    make_rig,
    pr_merged_event,
)

pytestmark = pytest.mark.unit
EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_merge_override_alarm_effect/overlay.example.yaml"
)
LANDING = EnumMergeOverrideVerdict.LANDING


BASE: dict[str, object] = {
    "pending_read_timeout_seconds": 600,
    "sweep_interval_seconds": 60,
}


def _verdict(result: ModelMergeOverrideResult) -> ModelMergeOverrideVerdictEvent:
    assert result.verdict is not None
    return result.verdict


def test_settings_without_login_load_while_no_repository_is_in_s19() -> None:
    settings = ModelMergeOverrideSettings.model_validate(
        {**BASE, "s19_repositories": []}
    )
    assert settings.landing_app_login is None


def test_settings_with_s19_repository_and_no_login_are_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ModelMergeOverrideSettings.model_validate({**BASE, "s19_repositories": [REPO]})
    assert "landing_app_login" in str(excinfo.value)


def test_golden_chain_landing_merge_without_login_before_s19(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(
        monkeypatch,
        tmp_path,
        settings={**BASE, "s19_repositories": []},
    )
    rig.handler.handle(arm_receipt())
    verdict = _verdict(drive_merge(rig))
    assert verdict.verdict is LANDING


def test_error_chain_s19_overlay_without_login_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(
        monkeypatch,
        tmp_path,
        settings={**BASE, "s19_repositories": [REPO]},
    )
    result = rig.handler.handle(pr_merged_event())
    assert result.outcome.value == "refused"
    assert rig.publisher.calls == []


def test_example_overlay_loads_through_the_model_with_and_without_the_login() -> None:
    example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert ModelMergeOverrideSettings.model_validate(example).landing_app_login
    without = {k: v for k, v in example.items() if k != "landing_app_login"}
    assert ModelMergeOverrideSettings.model_validate(without).landing_app_login is None
