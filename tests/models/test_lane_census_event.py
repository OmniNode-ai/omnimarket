# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane-census events moved into omnimarket: parity with the old builders (OMN-24425).

The expected events in ``fixtures/lane_census_event_parity.json`` were computed by running the old
``scripts/lane_census_event.py`` and ``scripts/lane_census_plan.py`` at omnibase_infra origin/dev
(441a2cf38). Every golden test below is a parity reading; the failing controls show the validator red
for a broken event.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.lane_census_event import (
    EVENT_SCHEMA_VERSION,
    FINDING_KIND_SEVERITY,
    alert_key,
    build_event,
    build_observed_event,
    ticket_title,
    validate_event,
)

_FIXTURE: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "lane_census_event_parity.json").read_text()
)
_NOW = datetime.fromisoformat(_FIXTURE["now"])
_CASES: dict[str, Any] = _FIXTURE["cases"]
_DRIFT_TOPIC: str = _CASES["prod_outage"]["drift"]["topic"]
_OBSERVED_TOPIC: str = _CASES["prod_outage"]["observed"]["topic"]


def _finding(kind: str, severity: str) -> dict[str, str]:
    return {
        "lane": "dev",
        "kind": kind,
        "container": "c",
        "detail": "d",
        "severity": severity,
    }


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_CASES))
def test_golden_chain_drift_and_observed_events_equal_the_old_builders(
    name: str,
) -> None:
    case = _CASES[name]
    drift = build_event(
        host=case["host"],
        plan=case["plan"],
        topic=case["drift"]["topic"],
        now=_NOW,
    )
    assert drift == case["drift"]
    assert (
        build_observed_event(
            host=case["host"],
            plan=case["plan"],
            topic=case["observed"]["topic"],
            now=_NOW,
        )
        == case["observed"]
    )


@pytest.mark.unit
def test_kind_table_equals_the_old_planner_table() -> None:
    assert _FIXTURE["kind_severity"] == FINDING_KIND_SEVERITY


@pytest.mark.unit
def test_golden_chain_a_built_event_validates_clean() -> None:
    event = build_event(
        host=_CASES["prod_outage"]["host"],
        plan=_CASES["prod_outage"]["plan"],
        topic=_DRIFT_TOPIC,
        now=_NOW,
    )
    assert validate_event(event, kind_severity=FINDING_KIND_SEVERITY) == []


@pytest.mark.unit
def test_error_chain_a_broken_event_reads_the_same_errors_as_the_old_validator() -> (
    None
):
    got = validate_event(
        _FIXTURE["validate"]["bad_event"], kind_severity=FINDING_KIND_SEVERITY
    )
    assert got == _FIXTURE["validate"]["bad"]
    assert got


@pytest.mark.unit
def test_failing_control_unknown_kind_and_wrong_severity_and_bool_count_are_errors() -> (
    None
):
    base = build_event(
        host="h", plan=_CASES["prod_outage"]["plan"], topic=_DRIFT_TOPIC, now=_NOW
    )
    unknown = {
        **base,
        "findings": [_finding("bogus_kind", "warning")],
        "drift_count": 1,
    }
    assert any(
        "bogus_kind" in e
        for e in validate_event(unknown, kind_severity=FINDING_KIND_SEVERITY)
    )
    wrong = {
        **base,
        "findings": [_finding("container_absent", "warning")],
        "drift_count": 1,
    }
    assert any(
        "container_absent" in e and "severity" in e
        for e in validate_event(wrong, kind_severity=FINDING_KIND_SEVERITY)
    )
    boolean = {**base, "drift_count": True}
    assert any(
        "drift_count" in e
        for e in validate_event(boolean, kind_severity=FINDING_KIND_SEVERITY)
    )


@pytest.mark.unit
def test_observed_event_is_not_an_alert_and_carries_its_own_topic() -> None:
    case = _CASES["prod_outage"]
    event = build_observed_event(
        host=case["host"],
        plan=case["plan"],
        topic=case["observed"]["topic"],
        now=_NOW,
    )
    assert event["topic"] == _OBSERVED_TOPIC
    assert event["schema_version"] == EVENT_SCHEMA_VERSION
    assert "observed_at" in event
    for key in ("alert_key", "ticket_title", "ticket_body", "emitted_at"):
        assert key not in event


@pytest.mark.unit
def test_drift_event_severity_is_critical_only_when_a_finding_is_critical() -> None:
    critical = build_event(
        host="h",
        plan={"findings": [_finding("container_absent", "critical")]},
        topic=_DRIFT_TOPIC,
        now=_NOW,
    )
    warning = build_event(
        host="h",
        plan={"findings": [_finding("image_tag_mismatch", "warning")]},
        topic=_DRIFT_TOPIC,
        now=_NOW,
    )
    assert critical["severity"] == "critical"
    assert warning["severity"] == "warning"
    assert critical["topic"] == _DRIFT_TOPIC


@pytest.mark.unit
def test_alert_key_is_stable_for_one_finding_set_and_changes_with_it() -> None:
    plan = _CASES["prod_outage"]["plan"]
    fewer = {**plan, "findings": plan["findings"][:1]}
    assert alert_key("h", plan) == alert_key("h", plan)
    assert alert_key("h", plan).startswith("lane-census-drift:h:")
    assert alert_key("h", plan) != alert_key("h", fewer)
    assert alert_key("h", plan) != alert_key("other", plan)


@pytest.mark.unit
def test_ticket_title_names_the_lane_and_every_kind_and_no_drift_reads_plainly() -> (
    None
):
    title = ticket_title(_CASES["prod_outage"]["plan"])
    assert "prod" in title
    assert "network_detached" in title
    assert "container_absent" in title
    assert ticket_title({"findings": []}) == "lane census: no drift"
