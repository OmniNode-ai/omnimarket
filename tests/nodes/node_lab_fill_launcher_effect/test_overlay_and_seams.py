# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay and the seams of the lab-fill launcher (OMN-24109).

Seam: the one JSON line this entry prints and the morning tick reads. The seams of the version-1 result file and of
lab-fill-latest.json had the removed fill's status step as the writer under test (OMN-24579) and are no longer tested here.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import cast

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_fill_launcher_effect.models import (
    ModelLabFillLauncherResult,
)
from omnimarket.nodes.node_lab_fill_launcher_effect.overlay import (
    ModelLabFillLauncherOverlay,
)

pytestmark = pytest.mark.unit

FIRE_ID = "20261113T060000Z-lab-fill"


def _example() -> dict[str, object]:
    text = (
        files("omnimarket.nodes.node_lab_fill_launcher_effect")
        .joinpath("overlay.example.yaml")
        .read_text("utf-8")
    )
    document: dict[str, object] = yaml.safe_load(text)
    return document


def test_overlay_example_loads_through_the_overlay_model() -> None:
    overlay = ModelLabFillLauncherOverlay.model_validate(_example())
    assert overlay.policy.workflow == "lab-fill"
    assert overlay.state.latest_filename == "lab-fill-latest.json"


@pytest.mark.parametrize("block", ["state", "alert", "policy"])
def test_overlay_refuses_a_missing_block(block: str) -> None:
    document = _example()
    del document[block]
    with pytest.raises(ValidationError):
        ModelLabFillLauncherOverlay.model_validate(document)


def test_overlay_refuses_a_missing_policy_key() -> None:
    document = _example()
    policy = dict(cast("dict[str, object]", document["policy"]))
    del policy["class_blind"]
    document["policy"] = policy
    with pytest.raises(ValidationError):
        ModelLabFillLauncherOverlay.model_validate(document)


def test_overlay_refuses_an_unknown_key() -> None:
    document = _example()
    document["surprise"] = 1
    with pytest.raises(ValidationError):
        ModelLabFillLauncherOverlay.model_validate(document)


def test_golden_chain_contract_declares_its_terminal_events() -> None:
    """Golden chain: the packaged contract declares its terminal event and topics."""
    text = (
        files("omnimarket.nodes.node_lab_fill_launcher_effect")
        .joinpath("contract.yaml")
        .read_text("utf-8")
    )
    contract: dict[str, object] = yaml.safe_load(text)
    topics = cast("dict[str, object]", contract["topics"])
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.lab-fill-launcher-completed.v1"
    )
    assert (
        topics["lab_fill_launcher_completed"]
        == "onex.evt.omnimarket.lab-fill-launcher-completed.v1"
    )
    assert (
        topics["lab_fill_launcher_failed"]
        == "onex.evt.omnimarket.lab-fill-launcher-failed.v1"
    )
    assert (
        topics["lab_fill_launcher_requested"]
        == "onex.cmd.omnimarket.lab-fill-launcher-requested.v1"
    )


def test_seam_tick_line_keys_are_what_the_tick_reads() -> None:
    """Seam 3: morning_workflows_tick.sh reads exactly these keys from the entry's one line."""
    assert set(ModelLabFillLauncherResult.model_fields) == {
        "phase",
        "exit",
        "degraded_class",
        "result_channel",
        "result_truncated",
        "notice_line",
        "alert_exit",
        "alert_category",
    }
    line = ModelLabFillLauncherResult(
        phase="completed",
        exit=0,
        degraded_class=None,
        result_channel="read",
        result_truncated=False,
        notice_line="",
        alert_exit=None,
        alert_category=None,
    ).tick_line()
    assert "\n" not in line
    assert json.loads(line)["degraded_class"] is None
