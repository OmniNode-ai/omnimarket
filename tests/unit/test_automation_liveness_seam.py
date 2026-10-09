# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Conformance tests for the automation-liveness seam (OMN-20793).

The seam is the overlay entry every automatic process declares, the overlay
that holds them, the bus events the observers, the projection and the watchdog
exchange, and the topic contract that names those events. Every later task of
the monitoring plan builds against these shapes and the fixtures beside them,
so the fixtures are frozen once merged.

Test names carry the acceptance-criterion selector they settle:
``automation_liveness_models`` (AC1), ``automation_liveness_deadline_arithmetic``
(AC2), ``automation_liveness_topics_format`` (AC3) and
``automation_liveness_default_overlay_neutral`` (AC4).
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_liveness_state import EnumLivenessState
from omnibase_core.validation.validator_topic_suffix import validate_topic_suffix
from pydantic import BaseModel, ValidationError

from omnimarket.models.liveness.model_automation_liveness import (
    EVENT_PAYLOAD_MODELS,
    VERDICT_REASONS,
    VERDICT_STATE,
    EnumAutomationLivenessEvent,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationTriggerKind,
    ModelAutomationAlarmDelivered,
    ModelAutomationAlarmRaised,
    ModelAutomationLivenessDeclared,
    ModelAutomationLivenessOverlay,
    ModelAutomationLivenessTiming,
    ModelAutomationLivenessVerdictEvent,
    ModelAutomationRunObserved,
    automation_liveness_topics,
    load_automation_liveness_overlay,
    load_default_automation_liveness_overlay,
    trigger_kind_slack_seconds,
)

_REPO = Path(__file__).resolve().parents[2]
_FIXTURES = _REPO / "tests" / "fixtures" / "automation_liveness"
_PACKAGE = "omnimarket.models.liveness"
_SCHEMA_FILE = "automation_liveness_overlay.schema.json"
_DEFAULT_OVERLAY_FILE = "automation_liveness_overlay.default.yaml"
_TOPIC_CONTRACT_FILE = "automation_liveness.contract.yaml"


def _yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), path
    return data


def _json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    assert isinstance(data, dict), path
    return data


def _base_overlay() -> dict[str, Any]:
    """The launchd-interval fixture: a valid overlay with one entry."""
    return _yaml(_FIXTURES / "trigger_kinds" / "launchd-interval.yaml")


def _entry(raw: dict[str, Any]) -> dict[str, Any]:
    processes = raw["processes"]
    assert isinstance(processes, list)
    entry = processes[0]
    assert isinstance(entry, dict)
    return entry


def _round_trip(model: type[BaseModel], data: dict[str, Any]) -> None:
    first = model.model_validate(data)
    dumped = first.model_dump(mode="json")
    second = model.model_validate(dumped)
    assert first == second
    assert second.model_dump(mode="json") == dumped


# --------------------------------------------------------------------------- AC1


@pytest.mark.unit
@pytest.mark.parametrize("kind", list(EnumAutomationTriggerKind), ids=str)
def test_automation_liveness_models_trigger_fixture_round_trips(
    kind: EnumAutomationTriggerKind,
) -> None:
    path = _FIXTURES / "trigger_kinds" / f"{kind.value}.yaml"
    assert path.is_file(), f"no fixture for trigger kind {kind.value}"
    raw = _yaml(path)
    _round_trip(ModelAutomationLivenessOverlay, raw)
    overlay = load_automation_liveness_overlay(path)
    assert [entry.trigger.kind for entry in overlay.processes] == [kind]


@pytest.mark.unit
@pytest.mark.parametrize("verdict", list(EnumAutomationLivenessVerdict), ids=str)
def test_automation_liveness_models_verdict_fixture_round_trips(
    verdict: EnumAutomationLivenessVerdict,
) -> None:
    path = _FIXTURES / "verdicts" / f"{verdict.value}.json"
    assert path.is_file(), f"no fixture for verdict {verdict.value}"
    raw = _json(path)
    _round_trip(ModelAutomationLivenessVerdictEvent, raw)
    event = ModelAutomationLivenessVerdictEvent.model_validate(raw)
    assert event.verdict is verdict
    assert event.state is VERDICT_STATE[verdict]


@pytest.mark.unit
@pytest.mark.parametrize("event", list(EnumAutomationLivenessEvent), ids=str)
def test_automation_liveness_models_event_fixture_round_trips(
    event: EnumAutomationLivenessEvent,
) -> None:
    path = _FIXTURES / "events" / f"{event.value}.json"
    assert path.is_file(), f"no fixture for event {event.value}"
    _round_trip(EVENT_PAYLOAD_MODELS[event], _json(path))


@pytest.mark.unit
def test_automation_liveness_models_fixture_dirs_hold_nothing_extra() -> None:
    kinds = {p.stem for p in (_FIXTURES / "trigger_kinds").glob("*.yaml")}
    verdicts = {p.stem for p in (_FIXTURES / "verdicts").glob("*.json")}
    events = {p.stem for p in (_FIXTURES / "events").glob("*.json")}
    assert kinds == {k.value for k in EnumAutomationTriggerKind}
    assert verdicts == {v.value for v in EnumAutomationLivenessVerdict}
    assert events == {e.value for e in EnumAutomationLivenessEvent}


@pytest.mark.unit
@pytest.mark.parametrize(
    "control",
    [
        None,
        {},
        {"kind": "replay_fixture", "ref": ""},
        {"kind": "class_drill", "ref": "   "},
    ],
    ids=["null", "empty-mapping", "empty-ref", "blank-ref"],
)
def test_automation_liveness_models_refuse_empty_positive_control(
    control: dict[str, str] | None,
) -> None:
    raw = _base_overlay()
    ModelAutomationLivenessOverlay.model_validate(raw)  # positive control
    _entry(raw)["positive_control"] = control
    with pytest.raises(ValidationError, match="positive_control"):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_models_refuse_missing_positive_control() -> None:
    raw = _base_overlay()
    del _entry(raw)["positive_control"]
    with pytest.raises(ValidationError, match="positive_control"):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_models_verdict_states_reuse_core_enum() -> None:
    assert set(VERDICT_STATE) == set(EnumAutomationLivenessVerdict)
    assert all(isinstance(s, EnumLivenessState) for s in VERDICT_STATE.values())
    assert VERDICT_STATE[EnumAutomationLivenessVerdict.HEALTHY] is (
        EnumLivenessState.HEALTHY
    )
    assert VERDICT_STATE[EnumAutomationLivenessVerdict.MISSED] is (
        EnumLivenessState.STALE
    )
    assert VERDICT_STATE[EnumAutomationLivenessVerdict.FAILED] is EnumLivenessState.RED


@pytest.mark.unit
def test_automation_liveness_models_every_reason_belongs_to_one_verdict() -> None:
    assert set(VERDICT_REASONS) == set(EnumAutomationLivenessVerdict)
    seen: list[EnumAutomationLivenessReason] = []
    for reasons in VERDICT_REASONS.values():
        assert reasons, "every verdict names at least one reason"
        seen.extend(reasons)
    assert sorted(seen) == sorted(EnumAutomationLivenessReason)


@pytest.mark.unit
def test_automation_liveness_models_verdict_refuses_foreign_reason() -> None:
    raw = _json(_FIXTURES / "verdicts" / "missed.json")
    raw["reason"] = EnumAutomationLivenessReason.RUN_FAILED.value
    with pytest.raises(ValidationError, match="reason"):
        ModelAutomationLivenessVerdictEvent.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_models_verdict_refuses_wrong_state() -> None:
    raw = _json(_FIXTURES / "verdicts" / "missed.json")
    raw["state"] = EnumLivenessState.HEALTHY.value
    with pytest.raises(ValidationError, match="state"):
        ModelAutomationLivenessVerdictEvent.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_models_finished_run_requires_outcome() -> None:
    raw = _json(_FIXTURES / "events" / "run_observed.json")
    assert raw["phase"] == "finished"
    raw["outcome"] = None
    with pytest.raises(ValidationError, match="outcome"):
        ModelAutomationRunObserved.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_models_started_run_carries_no_result() -> None:
    raw = _json(_FIXTURES / "events" / "run_observed.json")
    raw["phase"] = "started"
    with pytest.raises(ValidationError, match="started"):
        ModelAutomationRunObserved.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_models_delivery_needs_its_receipt() -> None:
    raw = _json(_FIXTURES / "events" / "alarm_delivered.json")
    assert raw["delivered"] is True
    raw["message_ref"] = None
    with pytest.raises(ValidationError, match="message_ref"):
        ModelAutomationAlarmDelivered.model_validate(raw)
    failed = _json(_FIXTURES / "events" / "alarm_delivered.json")
    failed["delivered"] = False
    with pytest.raises(ValidationError, match="failure"):
        ModelAutomationAlarmDelivered.model_validate(failed)


@pytest.mark.unit
def test_automation_liveness_models_low_severity_only_for_one_failed_run() -> None:
    raw = _json(_FIXTURES / "events" / "alarm_raised.json")
    assert raw["verdict"] == "missed"
    raw["severity"] = "low"
    with pytest.raises(ValidationError, match="severity"):
        ModelAutomationAlarmRaised.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_models_alarm_refuses_passing_verdict() -> None:
    raw = _json(_FIXTURES / "events" / "alarm_raised.json")
    raw["verdict"] = "healthy"
    raw["state"] = "healthy"
    raw["reason"] = "inside_bounds"
    with pytest.raises(ValidationError, match="alarm"):
        ModelAutomationAlarmRaised.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_models_declared_digest_must_match_overlay() -> None:
    raw = _json(_FIXTURES / "events" / "liveness_declared.json")
    ModelAutomationLivenessDeclared.model_validate(raw)  # positive control
    raw["overlay_digest"] = "0" * 64
    with pytest.raises(ValidationError, match="digest"):
        ModelAutomationLivenessDeclared.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_models_refuse_duplicate_process_id() -> None:
    raw = _base_overlay()
    raw["processes"].append(dict(_entry(raw)))
    with pytest.raises(ValidationError, match="duplicate"):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_models_refuse_unknown_field() -> None:
    raw = _base_overlay()
    _entry(raw)["mute"] = True
    with pytest.raises(ValidationError, match="mute"):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_models_schema_file_matches_model() -> None:
    shipped = json.loads(
        resources.files(_PACKAGE).joinpath(_SCHEMA_FILE).read_text(encoding="utf-8")
    )
    generated = ModelAutomationLivenessOverlay.model_json_schema(mode="validation")
    assert shipped == generated, (
        f"{_SCHEMA_FILE} drifted from the model; regenerate it from "
        "ModelAutomationLivenessOverlay.model_json_schema(mode='validation')"
    )
    assert "deadline" in json.dumps(shipped["x-onex-overlay-checks"])


# --------------------------------------------------------------------------- AC2


def _timing() -> ModelAutomationLivenessTiming:
    return ModelAutomationLivenessTiming()


@pytest.mark.unit
def test_automation_liveness_deadline_arithmetic_overhead_is_the_plan_sum() -> None:
    timing = _timing()
    assert timing.overhead_seconds == (
        timing.observer_interval_seconds
        + timing.confirmation_evaluations * timing.evaluation_interval_seconds
        + timing.outbox_retry_seconds
        + timing.delivery_budget_seconds
    )
    assert timing.delivery_budget_seconds == 120


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value", "verdict"),
    [
        ("max_silence_seconds", 100_000, "missed"),
        ("max_runtime_seconds", 100_000, "failed"),
        ("idle_runs_before_alarm", 1_000, "idle_with_demand"),
    ],
)
def test_automation_liveness_deadline_arithmetic_refuses_bound_past_deadline(
    field: str, value: int, verdict: str
) -> None:
    raw = _base_overlay()
    ModelAutomationLivenessOverlay.model_validate(raw)  # positive control
    _entry(raw)[field] = value
    with pytest.raises(
        ValidationError, match=f"deadline.*{verdict}|{verdict}.*deadline"
    ):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_deadline_arithmetic_overrun_shares_runtime_bound() -> None:
    overlay = ModelAutomationLivenessOverlay.model_validate(_base_overlay())
    bounds = overlay.processes[0].worst_case_seconds(overlay.timing)
    assert (
        bounds[EnumAutomationLivenessVerdict.OVERRUN]
        == (bounds[EnumAutomationLivenessVerdict.FAILED])
    )


@pytest.mark.unit
def test_automation_liveness_deadline_arithmetic_refuses_unanswered_trigger() -> None:
    raw = _yaml(_FIXTURES / "trigger_kinds" / "gh-event.yaml")
    ModelAutomationLivenessOverlay.model_validate(raw)  # positive control
    _entry(raw)["correlation"]["answer_deadline_seconds"] = 1_000_000
    with pytest.raises(ValidationError, match="trigger_unanswered"):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_deadline_arithmetic_boundary_is_inclusive() -> None:
    raw = _base_overlay()
    overlay = ModelAutomationLivenessOverlay.model_validate(raw)
    worst = max(overlay.processes[0].worst_case_seconds(overlay.timing).values())
    _entry(raw)["alarm_deadline_seconds"] = worst
    ModelAutomationLivenessOverlay.model_validate(raw)
    _entry(raw)["alarm_deadline_seconds"] = worst - 1
    with pytest.raises(ValidationError, match="deadline"):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_deadline_arithmetic_idle_unreachable_without_demand() -> (
    None
):
    raw = _base_overlay()
    _entry(raw)["idle_runs_before_alarm"] = 1_000
    _entry(raw)["demand"] = "none"
    overlay = ModelAutomationLivenessOverlay.model_validate(raw)
    bounds = overlay.processes[0].worst_case_seconds(overlay.timing)
    assert EnumAutomationLivenessVerdict.IDLE_WITH_DEMAND not in bounds


@pytest.mark.unit
@pytest.mark.parametrize("kind", list(EnumAutomationTriggerKind), ids=str)
def test_automation_liveness_deadline_arithmetic_fixtures_fit_their_deadline(
    kind: EnumAutomationTriggerKind,
) -> None:
    overlay = load_automation_liveness_overlay(
        _FIXTURES / "trigger_kinds" / f"{kind.value}.yaml"
    )
    entry = overlay.processes[0]
    bounds = entry.worst_case_seconds(overlay.timing)
    assert bounds, "every entry reaches at least one alarming verdict"
    assert max(bounds.values()) <= entry.alarm_deadline_seconds


@pytest.mark.unit
def test_automation_liveness_deadline_arithmetic_cron_needs_completion_evidence() -> (
    None
):
    raw = _yaml(_FIXTURES / "trigger_kinds" / "cron.yaml")
    ModelAutomationLivenessOverlay.model_validate(raw)  # positive control
    _entry(raw)["evidence"]["completion_record"] = None
    with pytest.raises(ValidationError, match="completion"):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_deadline_arithmetic_latest_only_needs_slow_cadence() -> (
    None
):
    raw = _base_overlay()
    timing = _timing()
    entry = _entry(raw)
    entry["run_record"] = "latest-only"
    entry["expected_interval_seconds"] = 3 * timing.observer_interval_seconds
    entry["max_silence_seconds"] = None
    ModelAutomationLivenessOverlay.model_validate(raw)  # exactly every third poll
    entry["expected_interval_seconds"] = 3 * timing.observer_interval_seconds - 1
    with pytest.raises(ValidationError, match="latest-only"):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_deadline_arithmetic_latest_only_scales_with_poll() -> None:
    raw = _base_overlay()
    entry = _entry(raw)
    entry["run_record"] = "latest-only"
    entry["expected_interval_seconds"] = 300
    entry["max_silence_seconds"] = None
    ModelAutomationLivenessOverlay.model_validate(raw)
    raw["timing"] = {"observer_interval_seconds": 120}
    with pytest.raises(ValidationError, match="latest-only"):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_deadline_arithmetic_latest_only_is_observer_only() -> None:
    raw = _base_overlay()
    entry = _entry(raw)
    entry["run_record"] = "latest-only"
    entry["emitter"] = "self"
    with pytest.raises(ValidationError, match="latest-only"):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_deadline_arithmetic_default_silence() -> None:
    raw = _base_overlay()
    entry = _entry(raw)
    entry["max_silence_seconds"] = None
    overlay = ModelAutomationLivenessOverlay.model_validate(raw)
    interval = entry["expected_interval_seconds"]
    assert overlay.processes[0].max_silence_seconds == 2 * interval
    assert trigger_kind_slack_seconds(EnumAutomationTriggerKind.GH_SCHEDULE, 3600) == (
        3600 + 90 * 60
    )
    gh = _yaml(_FIXTURES / "trigger_kinds" / "gh-schedule.yaml")
    _entry(gh)["max_silence_seconds"] = None
    gh_overlay = ModelAutomationLivenessOverlay.model_validate(gh)
    gh_interval = _entry(gh)["expected_interval_seconds"]
    assert gh_overlay.processes[0].max_silence_seconds == (
        2 * gh_interval + gh_interval + 90 * 60
    )


@pytest.mark.unit
def test_automation_liveness_deadline_arithmetic_event_trigger_needs_correlation() -> (
    None
):
    raw = _yaml(_FIXTURES / "trigger_kinds" / "gh-event.yaml")
    _entry(raw)["correlation"] = None
    with pytest.raises(ValidationError, match="correlation"):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_deadline_arithmetic_silence_not_below_interval() -> None:
    raw = _base_overlay()
    entry = _entry(raw)
    entry["max_silence_seconds"] = entry["expected_interval_seconds"] - 1
    with pytest.raises(ValidationError, match="max_silence_seconds"):
        ModelAutomationLivenessOverlay.model_validate(raw)


# --------------------------------------------------------------------------- AC3


def _topic_contract() -> dict[str, Any]:
    text = (
        resources.files(_PACKAGE)
        .joinpath(_TOPIC_CONTRACT_FILE)
        .read_text(encoding="utf-8")
    )
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


@pytest.mark.unit
def test_automation_liveness_topics_format_every_topic_passes() -> None:
    topics = automation_liveness_topics()
    assert set(topics) == set(EnumAutomationLivenessEvent)
    for event, topic in topics.items():
        result = validate_topic_suffix(topic)
        assert result.is_valid, f"{event.value}: {topic}: {result.error}"
        assert result.parsed is not None
        assert result.parsed.kind == "evt"
        assert result.parsed.producer == "omnimarket"
        assert result.parsed.event_name.startswith("automation-")
    assert len(set(topics.values())) == len(topics)


@pytest.mark.unit
def test_automation_liveness_topics_format_check_refuses_snake_producer() -> None:
    # Positive control: the same check refuses the shape the pr-watcher topic has.
    bad = ".".join(
        ("onex", "evt", "omnibase_internal", "automation-run-observed", "v1")
    )
    assert not validate_topic_suffix(bad).is_valid


@pytest.mark.unit
def test_automation_liveness_topics_format_contract_names_payload_models() -> None:
    contract = _topic_contract()
    declared = contract["event_topics"]
    assert set(declared) == {e.value for e in EnumAutomationLivenessEvent}
    for key, spec in declared.items():
        model = EVENT_PAYLOAD_MODELS[EnumAutomationLivenessEvent(key)]
        assert spec["payload_model"] == f"{model.__module__}.{model.__qualname__}"
        assert (
            spec["topic"]
            == automation_liveness_topics()[EnumAutomationLivenessEvent(key)]
        )


# --------------------------------------------------------------------------- AC4


_NEUTRAL_KEYS = frozenset({"schema_version", "processes"})


def _is_neutral(raw: dict[str, Any]) -> bool:
    """A neutral overlay declares a schema version and an empty process list."""
    return set(raw) <= _NEUTRAL_KEYS and raw.get("processes") == []


@pytest.mark.unit
def test_automation_liveness_default_overlay_neutral() -> None:
    text = (
        resources.files(_PACKAGE)
        .joinpath(_DEFAULT_OVERLAY_FILE)
        .read_text(encoding="utf-8")
    )
    raw = yaml.safe_load(text)
    assert isinstance(raw, dict)
    assert _is_neutral(raw), raw
    overlay = load_default_automation_liveness_overlay()
    assert overlay.processes == ()
    assert overlay == ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_default_overlay_neutral_check_has_teeth() -> None:
    # Positive control: an overlay declaring a host, unit or lane is not neutral.
    assert not _is_neutral(_base_overlay())
    assert not _is_neutral({"schema_version": "x", "processes": [], "lane": "x"})


# ----------------------------------------------------- review findings (Codex)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("observer_interval_seconds", 1),
        ("evaluation_interval_seconds", 1),
        ("confirmation_evaluations", 1),
        ("outbox_retry_seconds", 0),
        ("delivery_budget_seconds", 1),
    ],
)
def test_automation_liveness_deadline_arithmetic_timing_cannot_undercut_floor(
    field: str, value: int
) -> None:
    raw = _base_overlay()
    raw["timing"] = {field: value}
    with pytest.raises(ValidationError, match=field):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_deadline_arithmetic_cron_self_needs_completion() -> None:
    raw = _yaml(_FIXTURES / "trigger_kinds" / "cron.yaml")
    entry = _entry(raw)
    entry["emitter"] = "self"
    ModelAutomationLivenessOverlay.model_validate(raw)  # evidence still named
    del entry["evidence"]
    with pytest.raises(ValidationError, match="completion"):
        ModelAutomationLivenessOverlay.model_validate(raw)


@pytest.mark.unit
def test_automation_liveness_models_overlay_must_list_processes() -> None:
    with pytest.raises(ValidationError, match="processes"):
        ModelAutomationLivenessOverlay.model_validate(
            {"schema_version": _base_overlay()["schema_version"]}
        )


@pytest.mark.unit
def test_automation_liveness_models_json_schema_refuses_blank_strings() -> None:
    import jsonschema

    schema = json.loads(
        resources.files(_PACKAGE).joinpath(_SCHEMA_FILE).read_text(encoding="utf-8")
    )
    raw = _base_overlay()
    jsonschema.validate(raw, schema)  # positive control
    _entry(raw)["positive_control"]["ref"] = "   "
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(raw, schema)
