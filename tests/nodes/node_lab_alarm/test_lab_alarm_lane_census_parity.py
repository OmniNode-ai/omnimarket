# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity of the census helpers moved into node_lab_alarm with the old copies (OMN-24425).

The helpers (the drift-event validator, the alert key and the finding-kind severity table) moved unchanged from
the old alarm script's census module. Their expected results in ``fixtures/lane_census_parity.json`` were computed by
running the old functions, so a change to either side of the move shows here: a kind added to or re-rated in the
severity table, a key added to the event contract, or a different alert-key digest turns a case red.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_lab_alarm.handlers.lane_census_snapshot import (
    FINDING_KIND_SEVERITY,
    SUPPORTED_EVENT_SCHEMA_VERSIONS,
    alert_key,
    validate_event,
)

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lane_census_parity.json"


def _golden() -> dict[str, Any]:
    golden: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return golden


def test_census_parity_severity_table_equals_the_old_table() -> None:
    assert _golden()["severity"] == FINDING_KIND_SEVERITY


def test_census_parity_supported_versions_equal_the_old_ones() -> None:
    assert sorted(SUPPORTED_EVENT_SCHEMA_VERSIONS) == _golden()["versions"]


@pytest.mark.parametrize("name", sorted(_golden()["validate"]))
def test_census_parity_validate_event_returns_what_the_old_validator_returned(
    name: str,
) -> None:
    case = _golden()["validate"][name]
    assert (
        validate_event(case["event"], kind_severity=FINDING_KIND_SEVERITY)
        == case["errors"]
    )


def test_census_parity_the_golden_cases_hold_both_a_valid_and_an_invalid_event() -> (
    None
):
    """Positive control: the fixture carries a valid event (no errors) and invalid ones (errors), so an always-empty or always-failing validator turns a case red."""
    cases = _golden()["validate"]
    assert cases["valid"]["errors"] == []
    assert cases["old_version_ok"]["errors"] == []
    assert all(
        cases[n]["errors"] for n in cases if n not in ("valid", "old_version_ok")
    )


@pytest.mark.parametrize("name", sorted(_golden()["keys"]))
def test_census_parity_alert_key_returns_what_the_old_key_function_returned(
    name: str,
) -> None:
    case = _golden()["keys"][name]
    assert alert_key("host-1", {"findings": case["findings"]}) == case["alert_key"]


def test_census_parity_alert_key_ignores_order_and_detail_and_names_the_host() -> None:
    keys = _golden()["keys"]
    assert keys["two_ab"]["alert_key"] == keys["two_ba"]["alert_key"]
    assert keys["two_ab"]["alert_key"] == keys["detail_ignored"]["alert_key"]
    assert keys["one"]["alert_key"] != keys["two_ab"]["alert_key"]
    assert alert_key("host-2", {"findings": keys["one"]["findings"]}).startswith(
        "lane-census-drift:host-2:"
    )
