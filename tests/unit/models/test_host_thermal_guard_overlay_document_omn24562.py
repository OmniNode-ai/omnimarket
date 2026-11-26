# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The thermal guard's overlay document is a shared model the overlays repository can pin (OMN-24562).

The overlays repository pins a node's overlay to a merged revision that already holds the model, so the model lives
in the shared overlay documents package and merges ahead of the node that reads it.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_host_thermal_guard_overlay import (
    ModelHostThermalGuardOverlay,
)

_DOC: dict[str, Any] = {
    "host_env": "HOST_VAR",
    "hosts": [{"name": "host-a", "state_dir": "state/guard", "lane": "guard-lane-a"}],
    "hold_above_c": 85.0,
    "release_below_c": 82.0,
    "min_hold_s": 300,
    "stop_above_c": 90.0,
    "stop_report_every_s": 600,
    "hold_row_until_s": 21600,
    "hold_file": "hold.json",
    "episode_file": "episode.json",
    "hwmon_root": "/sys/class/hwmon",
    "lane_scope_pattern": "lane-*",
    "lane_tag_env": "LANE_TAG_VAR",
    "actor": "unit:guard",
    "ticket": "OMN-1",
    "ledger_append": {
        "command_env": "APPEND_COMMAND_VAR",
        "host_env": "LEDGER_HOST_VAR",
        "timeout_seconds": 60,
    },
    "hold_text": "{host} hold",
    "release_text": "{host} release",
    "stop_text": "{host} stop",
}


def _doc(**changes: Any) -> dict[str, Any]:
    return {**copy.deepcopy(_DOC), **changes}


def test_the_complete_document_loads() -> None:
    doc = ModelHostThermalGuardOverlay.model_validate(_doc())

    assert doc.host_row("host-a").lane == "guard-lane-a"
    assert doc.hold_above_c == 85.0
    assert doc.release_below_c == 82.0
    assert doc.ledger_append.timeout_seconds == 60


@pytest.mark.parametrize("key", sorted(_DOC))
def test_a_document_missing_a_key_is_refused(key: str) -> None:
    broken = {k: v for k, v in copy.deepcopy(_DOC).items() if k != key}

    with pytest.raises(ValidationError):
        ModelHostThermalGuardOverlay.model_validate(broken)


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelHostThermalGuardOverlay.model_validate(_doc(gateway_url="x"))


def test_a_stop_threshold_may_be_null_to_turn_the_cut_off() -> None:
    assert (
        ModelHostThermalGuardOverlay.model_validate(
            _doc(stop_above_c=None)
        ).stop_above_c
        is None
    )


def test_a_release_threshold_at_or_above_the_hold_threshold_is_refused() -> None:
    with pytest.raises(ValidationError, match="release_below_c must be below"):
        ModelHostThermalGuardOverlay.model_validate(_doc(release_below_c=85.0))


def test_a_stop_threshold_at_or_below_the_hold_threshold_is_refused() -> None:
    with pytest.raises(ValidationError, match="stop_above_c must be above"):
        ModelHostThermalGuardOverlay.model_validate(_doc(stop_above_c=85.0))


def test_a_host_listed_twice_is_refused() -> None:
    row = _DOC["hosts"][0]
    with pytest.raises(ValidationError, match="listed twice"):
        ModelHostThermalGuardOverlay.model_validate(_doc(hosts=[row, row]))


def test_a_host_the_document_does_not_list_is_refused_never_guessed() -> None:
    doc = ModelHostThermalGuardOverlay.model_validate(_doc())

    with pytest.raises(
        ValueError, match="not a host of the node_host_thermal_guard_effect"
    ):
        doc.host_row("host-b")
