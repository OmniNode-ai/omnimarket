# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A settings-store change to a model host or routing key runs the affected delegation checks and alarms on a red one.

The incident's recorded data is tests/fixtures/delegation_check_regression/incident_2026_11_23.json: the settings-store
version change of the delegation_routing overlay and the readings of the board's customer checks around the 13:30Z to
14:45Z transition on 2026-11-23.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    EnumDeliveryStep,
    ModelAlarmDelivery,
    ModelLabAlarmDeliveryRequest,
    ModelLabAlarmDeliveryResult,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.replay_source import (
    ReplayCheckSource,
)
from omnimarket.nodes.node_delegation_check_regression_effect.handlers.handler_check_regression import (
    HandlerCheckRegression,
)
from omnimarket.nodes.node_delegation_check_regression_effect.handlers.selection import (
    changed_keys,
    select_checks,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_check_regression import (
    EnumChangeKind,
    EnumCheckRegressionOutcome,
    EnumCheckVerdict,
    ModelCheckReading,
    ModelCheckRegressionRequest,
)
from omnimarket.nodes.node_delegation_check_regression_effect.overlay import (
    DEFAULTS_PATH,
    ModelCheckRegressionSettings,
    load_settings,
)

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "delegation_check_regression"
    / "incident_2026_11_23.json"
)


def _incident() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _settings() -> ModelCheckRegressionSettings:
    return load_settings(DEFAULTS_PATH)


class _RecordingDelivery:
    def __init__(self) -> None:
        self.requests: list[ModelLabAlarmDeliveryRequest] = []

    def handle(
        self, request: ModelLabAlarmDeliveryRequest
    ) -> ModelLabAlarmDeliveryResult:
        self.requests.append(request)
        return ModelLabAlarmDeliveryResult(
            correlation_id=request.correlation_id,
            condition=request.condition,
            outcome=EnumDeliveryOutcome.DELIVERED
            if request.alarms
            else EnumDeliveryOutcome.NOTHING_NEW,
            deliveries=tuple(
                ModelAlarmDelivery(
                    key=a.key,
                    channel=EnumDeliveryStep.DELIVERED,
                    ledger=EnumDeliveryStep.DELIVERED,
                )
                for a in request.alarms
            ),
            cleared=(),
            retried=(),
            state_kept=True,
        )


def _request(
    target: str = "dev", kind: str = "host_model"
) -> ModelCheckRegressionRequest:
    incident = _incident()
    if kind == "settings_store":
        change = incident["settings_change"]
        return ModelCheckRegressionRequest(
            correlation_id="delegation-check-regression-test",
            change_kind=EnumChangeKind.SETTINGS_STORE,
            subject=change["overlay_id"],
            previous_document=change["previous_document"],
            new_document=change["new_document"],
            applied_at=datetime.fromisoformat(change["applied_at"]),
            actor=change["loaded_by"],
            target=target,
        )
    change = incident[f"{kind}_change"]
    return ModelCheckRegressionRequest(
        correlation_id="delegation-check-regression-test",
        change_kind=EnumChangeKind(change["change_kind"]),
        subject=change["subject"],
        changed_keys=tuple(change["changed_keys"]),
        applied_at=datetime.fromisoformat(change["applied_at"]),
        actor=change["actor"],
        target=target,
    )


def _handler(
    source: ReplayCheckSource, delivery: _RecordingDelivery
) -> HandlerCheckRegression:
    return HandlerCheckRegression(
        settings=_settings(),
        runner=source,
        history=source,
        delivery=delivery,
        clock=lambda: datetime(2026, 11, 23, 13, 56, 25, tzinfo=UTC),
    )


def test_delegation_check_regression_selection_picks_c14_and_c29_for_the_recorded_change() -> (
    None
):
    change = _incident()["settings_change"]
    keys = changed_keys(change["previous_document"], change["new_document"])
    assert keys, "the recorded version change must show a changed key"
    assert all(k.startswith("local_endpoints/backends[3]") for k in keys)
    selected = select_checks(
        _settings(), EnumChangeKind.SETTINGS_STORE, change["overlay_id"], keys
    )
    assert "C14" in selected
    assert "C29" in selected


@pytest.mark.parametrize("kind", ["host_model", "release"])
def test_delegation_check_regression_selection_picks_c14_and_c29_for_the_recorded_qwen_and_release_changes(
    kind: str,
) -> None:
    request = _request(kind=kind)
    selected = select_checks(
        _settings(), request.change_kind, request.subject, request.changed_keys
    )
    assert {"C14", "C29"} <= set(selected)


def test_delegation_check_regression_a_key_no_rule_names_selects_nothing() -> None:
    selected = select_checks(
        _settings(),
        EnumChangeKind.SETTINGS_STORE,
        "delegation_routing",
        ("eval_arms/arms[0]/price_in_usd_per_mtok",),
    )
    assert selected == ()


def test_delegation_check_regression_a_red_check_raises_one_alarm_with_an_owner_and_green_raises_none() -> (
    None
):
    incident = _incident()
    source = ReplayCheckSource.from_readings(
        incident["readings"],
        previous_through=incident["transition_window"]["green_through"],
        current_through=incident["transition_window"]["red_from"],
    )
    delivery = _RecordingDelivery()
    result = _handler(source, delivery).handle(_request())
    assert result.outcome is EnumCheckRegressionOutcome.ALARM
    alarmed = {a.check_id for a in result.alarms}
    assert "C14" in alarmed
    assert "C29" in alarmed
    assert "C13" not in alarmed, "C13 reads green and raises nothing"
    assert len(result.alarms) == len(alarmed), "one alarm per red check"
    settings = _settings()
    for alarm in result.alarms:
        assert alarm.owner == settings.checks[alarm.check_id].owner
        assert alarm.owner.strip()
    sent = [r for r in delivery.requests if r.outcome is EnumDeliveryCondition.ALARM]
    assert len(sent) == 1
    assert {a.subject.split(":")[0] for a in sent[0].alarms} == alarmed
    assert all(settings.checks[c].owner in str(sent[0].alarms) for c in alarmed)


def test_delegation_check_regression_all_green_raises_no_alarm() -> None:
    readings = [{**r, "verdict": "green"} for r in _incident()["readings"]]
    source = ReplayCheckSource.from_readings(
        readings,
        previous_through="2026-11-23T13:30:00Z",
        current_through="2026-11-23T14:45:00Z",
    )
    delivery = _RecordingDelivery()
    result = _handler(source, delivery).handle(_request())
    assert result.outcome is EnumCheckRegressionOutcome.OK
    assert result.alarms == ()
    assert all(r.outcome is not EnumDeliveryCondition.ALARM for r in delivery.requests)
    assert {v.verdict for v in result.verdicts} == {EnumCheckVerdict.GREEN}


def test_delegation_check_regression_a_check_already_red_before_the_change_raises_nothing_new() -> (
    None
):
    readings = [{**r, "verdict": "red"} for r in _incident()["readings"]]
    source = ReplayCheckSource.from_readings(
        readings,
        previous_through="2026-11-23T13:30:00Z",
        current_through="2026-11-23T14:45:00Z",
    )
    delivery = _RecordingDelivery()
    result = _handler(source, delivery).handle(_request())
    assert result.alarms == ()


def test_delegation_check_regression_a_prod_target_is_refused_before_any_run_or_alarm() -> (
    None
):
    incident = _incident()
    source = ReplayCheckSource.from_readings(
        incident["readings"],
        previous_through=incident["transition_window"]["green_through"],
        current_through=incident["transition_window"]["red_from"],
    )
    delivery = _RecordingDelivery()
    result = _handler(source, delivery).handle(_request(target="prod"))
    assert result.outcome is EnumCheckRegressionOutcome.REFUSED
    assert "prod" in result.failure
    assert delivery.requests == []
    assert source.runs == []


def test_delegation_check_regression_overlay_defaults_declare_every_threshold_and_owner() -> (
    None
):
    settings = _settings()
    assert settings.alarm_deadline_seconds == 900
    assert "prod" in settings.refused_targets
    named = {c for rule in settings.key_check_map for c in rule.checks}
    assert {"C13", "C14", "C29"} <= named
    for check_id in named:
        check = settings.checks[check_id]
        assert check.owner.strip()
        assert check.read_deadline_seconds > 0
        assert check.red_streak_to_alarm >= 1


@pytest.mark.parametrize(
    "drop",
    [
        ("alarm_deadline_seconds",),
        ("refused_targets",),
        ("checks", "C14", "owner"),
        ("checks", "C14", "read_deadline_seconds"),
        ("checks", "C29", "red_streak_to_alarm"),
    ],
)
def test_delegation_check_regression_a_missing_threshold_or_owner_default_fails(
    drop: tuple[str, ...],
) -> None:
    raw = yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8"))
    node = raw
    for part in drop[:-1]:
        node = node[part]
    del node[drop[-1]]
    with pytest.raises(ValidationError):
        ModelCheckRegressionSettings.model_validate(raw)


def test_delegation_check_regression_defaults_that_accept_prod_are_refused() -> None:
    raw = deepcopy(yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8")))
    raw["refused_targets"] = ["staging"]
    with pytest.raises(ValidationError):
        ModelCheckRegressionSettings.model_validate(raw)


def test_delegation_check_regression_replays_the_recorded_transition_into_alarm_rows_for_c14_and_c29() -> (
    None
):
    """The regression test of the incident: it fails when the recorded red readings raise no alarm for C14 and C29."""
    incident = _incident()
    source = ReplayCheckSource.from_readings(
        incident["readings"],
        previous_through=incident["transition_window"]["green_through"],
        current_through=incident["transition_window"]["red_from"],
    )
    delivery = _RecordingDelivery()
    result = _handler(source, delivery).handle(_request())
    by_check = {a.check_id: a for a in result.alarms}
    assert set(by_check) == {"C14", "C29"}
    assert by_check["C14"].previous is EnumCheckVerdict.GREEN
    assert by_check["C14"].current is EnumCheckVerdict.RED
    assert by_check["C29"].run_ref == "45189064181"
    within = timedelta(seconds=_settings().alarm_deadline_seconds)
    assert all(a.raised_after <= within for a in result.alarms)


def test_delegation_check_regression_a_reading_past_the_deadline_still_alarms_and_says_so() -> (
    None
):
    incident = _incident()
    source = ReplayCheckSource.from_readings(
        incident["readings"],
        previous_through=incident["transition_window"]["green_through"],
        current_through=incident["transition_window"]["red_from"],
    )
    late = HandlerCheckRegression(
        settings=_settings(),
        runner=source,
        history=source,
        delivery=_RecordingDelivery(),
        clock=lambda: (
            datetime(2026, 11, 23, 13, 46, 25, tzinfo=UTC) + timedelta(seconds=2000)
        ),
    )
    result = late.handle(_request())
    assert {a.check_id for a in result.alarms} == {"C14", "C29"}
    assert all(a.late for a in result.alarms)


def test_delegation_check_regression_a_reading_that_is_green_clears_the_reading_type() -> (
    None
):
    reading = ModelCheckReading(
        check_id="C14",
        verdict=EnumCheckVerdict.GREEN,
        read_at=datetime(2026, 11, 23, 13, 30, tzinfo=UTC),
        run_ref="",
        source="x",
    )
    assert reading.verdict is EnumCheckVerdict.GREEN
