# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The exception queue persists the andon stop (T-A2): open, lease, countermeasure, clear, declare-false.

Every key, lane and role below is an invented fixture name or a role of the reducer's contract.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.rules_stop_gate import load_andon_stop_contract, stop_gate
from omnimarket.models.stop_gate import (
    EnumReadingOrigin,
    EnumReadingVerdict,
    EnumStopEventKind,
    EnumStopGateOutcome,
    EnumStopMode,
    ModelStopEvent,
    ModelStopGateRequest,
    ModelStopPolicy,
    ModelStopReading,
    ModelStopScope,
)
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_parity import (
    ModelScenarioRow,
    ModelScenarioRun,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
)
from omnimarket.nodes.node_exception_queue_effect import handler, models

pytestmark = pytest.mark.unit

OPEN, SWARMING, FIXED, STALE, CLEARED = "open", "swarming", "fixed", "stale", "cleared"
CLASS = "engine-policy-breach"
KEY = "example-engine"
NOW = "2026-11-01T10:00:00Z"
T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
FSM, CONFIG = load_andon_stop_contract()
PASS = ModelStopReading(
    verdict=EnumReadingVerdict.PASS,
    proof_eligible=True,
    origin=EnumReadingOrigin.EXECUTED,
    covers_scope=True,
)
ROLE_OF = {
    EnumStopEventKind.LEASE_GRANTED: "swarm_lane",
    EnumStopEventKind.LEASE_EXPIRED: "detector",
    EnumStopEventKind.COUNTERMEASURE_MERGED: "swarm_lane",
    EnumStopEventKind.READBACK: "readback",
    EnumStopEventKind.DECLARED_FALSE: "operator",
}


def row(cls: str = CLASS, key: str = KEY, **fields: Any) -> models.ModelExceptionRow:
    return models.ModelExceptionRow(
        cls=cls,
        key=key,
        source="detector-1",
        evidence="SM-02 reads mismatched > 0",
        tried="nothing, a detector only reads",
        proposed_fix="stop dispatch of the engine",
        readback_command="read the SM-02 line",
        **fields,
    )


def emit(
    state_dir: Path,
    r: models.ModelExceptionRow | None = None,
    role: str = "detector",
    now: str = NOW,
) -> models.ModelExceptionEmitResult:
    return handler.HandlerExceptionEmit().handle(
        models.ModelExceptionEmitRequest(state_dir, r or row(), now, role=role)
    )


def stop_event(
    stop_id: str,
    kind: EnumStopEventKind,
    minutes: int = 1,
    role: str | None = None,
    **fields: Any,
) -> ModelStopEvent:
    return ModelStopEvent(
        stop_id=stop_id,
        kind=kind,
        role=ROLE_OF[kind] if role is None else role,
        at=T0 + timedelta(minutes=minutes),
        **fields,
    )


def send(state_dir: Path, ev: ModelStopEvent) -> models.ModelStopEventResult:
    return handler.HandlerStopEvent().handle(
        models.ModelStopEventRequest(
            state_dir, ev, ev.at.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    )


def open_stops(state_dir: Path) -> models.ModelOpenStopsResult:
    return handler.HandlerOpenStops().handle(models.ModelOpenStopsRequest(state_dir))


def queue_lines(state_dir: Path) -> list[dict[str, Any]]:
    path = state_dir / handler.QUEUE_FILE
    return [json.loads(line) for line in path.read_text().splitlines()]


def decision_rows(state_dir: Path) -> list[dict[str, Any]]:
    return [r for r in queue_lines(state_dir) if r.get("decision") is True]


def gate(state_dir: Path, mode: EnumStopMode = EnumStopMode.ENFORCE) -> Any:
    return stop_gate(
        ModelStopGateRequest(
            scope=ModelStopScope(kind="engine", key=KEY),
            stops=open_stops(state_dir).stops,
            policy=ModelStopPolicy(modes={CLASS: mode}),
            now=T0 + timedelta(minutes=30),
        ),
        FSM,
    )


# opening: one stop per key, an occurrence per recurrence


def test_a_registered_class_opens_one_stop_and_a_repeat_is_the_same_stop(
    tmp_path: Path,
) -> None:
    first = emit(tmp_path)
    again = emit(tmp_path)
    assert first.status == "appended"
    assert first.stop == f"opened:{first.id}"
    assert again.status == "repeat"
    assert again.id == first.id
    assert again.occurrence == 1
    [stop] = open_stops(tmp_path).stops
    assert stop.id == first.id
    assert stop.state == OPEN
    assert stop.scope == ModelStopScope(kind="engine", key=KEY)
    assert stop.readback_command == "read the SM-02 line"
    assert len(queue_lines(tmp_path)) == 1


def test_a_recurrence_after_the_stop_cleared_is_occurrence_two(tmp_path: Path) -> None:
    first = emit(tmp_path)
    send(tmp_path, stop_event(first.id, EnumStopEventKind.DECLARED_FALSE))
    assert open_stops(tmp_path).stops == ()
    second = emit(tmp_path, now="2026-11-01T11:00:00Z")
    assert second.status == "appended"
    assert second.id != first.id
    assert second.occurrence == 2


# AC4: a class outside the registry opens a failure record and no stop


def test_ac4_a_class_outside_the_registry_opens_a_failure_record_and_no_stop(
    tmp_path: Path,
) -> None:
    result = emit(tmp_path, row(cls="not-a-registered-class"), role="")
    assert result.status == "appended"
    assert result.stop == "none:class_not_registered"
    assert open_stops(tmp_path).stops == ()
    [stored] = queue_lines(tmp_path)
    assert stored["cls"] == "not-a-registered-class"
    assert "stop" not in stored
    assert gate(tmp_path).outcome is EnumStopGateOutcome.ADMIT


def test_ac4_a_registered_class_from_the_wrong_role_opens_no_stop(
    tmp_path: Path,
) -> None:
    result = emit(tmp_path, role="operator")
    assert result.status == "refused"
    assert "role_not_allowed" in result.detail
    assert open_stops(tmp_path).stops == ()


# AC5: writer roles, refused at the queue's append and the refusal recorded


@pytest.mark.parametrize(
    ("kind", "extra"),
    [
        (EnumStopEventKind.LEASE_GRANTED, {"lane_id": "swarm-lane-1"}),
        (EnumStopEventKind.COUNTERMEASURE_MERGED, {}),
        (EnumStopEventKind.READBACK, {"reading": PASS}),
        (EnumStopEventKind.DECLARED_FALSE, {}),
    ],
)
def test_ac5_a_write_from_the_wrong_role_is_refused_and_the_refusal_recorded(
    tmp_path: Path, kind: EnumStopEventKind, extra: dict[str, Any]
) -> None:
    opened = emit(tmp_path)
    before = open_stops(tmp_path).stops
    wrong = "detector" if kind is not EnumStopEventKind.DECLARED_FALSE else "swarm_lane"
    result = send(tmp_path, stop_event(opened.id, kind, role=wrong, **extra))
    assert result.status == "refused"
    assert result.reason == f"role_not_allowed:{wrong}"
    assert open_stops(tmp_path).stops == before
    [refusal] = open_stops(tmp_path).refusals
    assert refusal["stop_refused"] == opened.id
    assert refusal["event"] == kind.value
    assert refusal["role"] == wrong
    assert refusal["reason"] == f"role_not_allowed:{wrong}"
    assert not decision_rows(tmp_path)


def test_ac5_a_write_from_the_right_role_is_accepted(tmp_path: Path) -> None:
    opened = emit(tmp_path)
    result = send(
        tmp_path,
        stop_event(opened.id, EnumStopEventKind.LEASE_GRANTED, lane_id="swarm-lane-1"),
    )
    assert result.status == "applied"
    [stop] = open_stops(tmp_path).stops
    assert stop.state == SWARMING
    assert stop.swarm_lane == "swarm-lane-1"
    assert open_stops(tmp_path).refusals == ()


def test_an_event_for_an_unknown_stop_is_refused_and_recorded(tmp_path: Path) -> None:
    result = send(
        tmp_path, stop_event("no-such-stop", EnumStopEventKind.DECLARED_FALSE)
    )
    assert result.status == "refused"
    assert result.reason == "unknown_stop"
    assert open_stops(tmp_path).refusals[0]["stop_refused"] == "no-such-stop"


# AC1 through the queue: refuse while open, admit once the readbacks cleared it


def test_ac1_the_gate_refuses_through_the_queue_until_the_stop_clears(
    tmp_path: Path,
) -> None:
    opened = emit(tmp_path)
    result = gate(tmp_path)
    assert result.outcome is EnumStopGateOutcome.REFUSE
    assert result.stop_id == opened.id
    send(
        tmp_path,
        stop_event(
            opened.id, EnumStopEventKind.LEASE_GRANTED, 1, lane_id="swarm-lane-1"
        ),
    )
    assert gate(tmp_path).swarm_lane == "swarm-lane-1"
    send(tmp_path, stop_event(opened.id, EnumStopEventKind.COUNTERMEASURE_MERGED, 2))
    first = send(
        tmp_path, stop_event(opened.id, EnumStopEventKind.READBACK, 3, reading=PASS)
    )
    assert first.status == "applied"
    assert first.stop is not None
    assert first.stop.state == FIXED
    assert gate(tmp_path).outcome is EnumStopGateOutcome.REFUSE
    last = send(
        tmp_path, stop_event(opened.id, EnumStopEventKind.READBACK, 4, reading=PASS)
    )
    assert last.stop is not None
    assert last.stop.state == CLEARED
    assert last.stop.cleared_at == T0 + timedelta(minutes=4)
    assert open_stops(tmp_path).stops == ()
    assert gate(tmp_path).outcome is EnumStopGateOutcome.ADMIT


def test_ac2_an_unknown_reading_through_the_queue_leaves_the_stop_open(
    tmp_path: Path,
) -> None:
    opened = emit(tmp_path)
    send(tmp_path, stop_event(opened.id, EnumStopEventKind.COUNTERMEASURE_MERGED, 1))
    unknown = ModelStopReading(verdict=EnumReadingVerdict.UNKNOWN)
    for minute in (2, 3, 4):
        send(
            tmp_path,
            stop_event(opened.id, EnumStopEventKind.READBACK, minute, reading=unknown),
        )
    [stop] = open_stops(tmp_path).stops
    assert stop.state == FIXED
    assert stop.cleared_at is None
    assert gate(tmp_path).outcome is EnumStopGateOutcome.REFUSE


def test_ac6_observe_mode_through_the_queue_admits_with_the_stop_named(
    tmp_path: Path,
) -> None:
    opened = emit(tmp_path)
    result = gate(tmp_path, EnumStopMode.OBSERVE)
    assert result.outcome is EnumStopGateOutcome.ADMIT
    assert result.stop_id == opened.id


# AC7: leases through the queue


def test_ac7_a_replacement_past_the_maximum_writes_one_decision_row(
    tmp_path: Path,
) -> None:
    opened = emit(tmp_path)
    minutes = 1
    send(
        tmp_path,
        stop_event(
            opened.id, EnumStopEventKind.LEASE_GRANTED, minutes, lane_id="lane-0"
        ),
    )
    step = CONFIG.lease_minutes + 1
    for replacement in range(CONFIG.max_lease_replacements):
        minutes += step
        result = send(
            tmp_path,
            stop_event(
                opened.id,
                EnumStopEventKind.LEASE_EXPIRED,
                minutes,
                lane_id=f"lane-{replacement + 1}",
            ),
        )
        assert result.status == "applied"
        assert decision_rows(tmp_path) == []
    minutes += step
    result = send(
        tmp_path,
        stop_event(
            opened.id, EnumStopEventKind.LEASE_EXPIRED, minutes, lane_id="lane-x"
        ),
    )
    assert result.stop is not None
    assert result.stop.state == STALE
    [decision] = decision_rows(tmp_path)
    assert decision["cls"] == "andon-stop-decision"
    assert opened.id in decision["key"]
    [stale] = open_stops(tmp_path).stops
    assert stale.state == STALE


def test_a_declared_false_stop_is_recorded_with_one_decision_row_and_leaves_the_queue(
    tmp_path: Path,
) -> None:
    opened = emit(tmp_path)
    send(tmp_path, stop_event(opened.id, EnumStopEventKind.DECLARED_FALSE))
    assert open_stops(tmp_path).stops == ()
    assert len(decision_rows(tmp_path)) == 1


def test_the_second_occurrence_still_starts_the_mechanize_lane_for_a_stop_class(
    tmp_path: Path,
) -> None:
    first = emit(tmp_path)
    send(tmp_path, stop_event(first.id, EnumStopEventKind.DECLARED_FALSE))
    second = emit(tmp_path, now="2026-11-01T11:00:00Z")
    assert second.occurrence == 2
    assert second.mechanize.startswith("unsent:")


def test_the_queue_read_and_list_ignore_stop_log_lines(tmp_path: Path) -> None:
    opened = emit(tmp_path)
    send(
        tmp_path,
        stop_event(
            opened.id,
            EnumStopEventKind.LEASE_GRANTED,
            role="detector",
            lane_id="lane-0",
        ),
    )
    send(
        tmp_path,
        stop_event(opened.id, EnumStopEventKind.LEASE_GRANTED, lane_id="lane-0"),
    )
    listed = handler.HandlerExceptionQueue().handle(
        models.ModelExceptionQueueRequest(tmp_path, "orchestrator")
    )
    assert [r["id"] for r in listed.rows] == [opened.id]
    read = handler.HandlerExceptionQueueRead().handle(
        models.ModelExceptionQueueReadRequest(tmp_path)
    )
    assert read.line.startswith("exceptions decision=0 open=1")


# AC8: the queue reads the registry and the roles from the contract it is given


def test_ac8_the_queue_reads_the_registry_from_the_contract_config(
    tmp_path: Path,
) -> None:
    fewer = CONFIG.model_copy(
        update={"stop_classes": tuple(c for c in CONFIG.stop_classes if c.id != CLASS)}
    )
    result = handler.HandlerExceptionEmit(config=fewer).handle(
        models.ModelExceptionEmitRequest(tmp_path, row(), NOW, role="detector")
    )
    assert result.stop == "none:class_not_registered"


def test_ac8_the_lease_length_comes_from_the_contract_config(tmp_path: Path) -> None:
    opened = emit(tmp_path)
    short = CONFIG.model_copy(update={"lease_minutes": 7})
    result = handler.HandlerStopEvent(config=short).handle(
        models.ModelStopEventRequest(
            tmp_path,
            stop_event(opened.id, EnumStopEventKind.LEASE_GRANTED, 1, lane_id="lane-0"),
            NOW,
        )
    )
    assert result.stop is not None
    assert result.stop.lease_expires_at == T0 + timedelta(minutes=1 + 7)


# AC9: the same semantic projection under the local and the lab profile

NOW_MIN = 0
CODES = ("LED", "STO", "CHG", "HST", "CLK")
SCENARIO: list[tuple[EnumStopEventKind, int, dict[str, Any]]] = [
    (EnumStopEventKind.LEASE_GRANTED, 1, {"lane_id": "lane-0"}),
    (EnumStopEventKind.COUNTERMEASURE_MERGED, 2, {}),
    (EnumStopEventKind.READBACK, 3, {"reading": PASS}),
    (EnumStopEventKind.READBACK, 4, {"reading": PASS}),
]
Facts = list[tuple[EnumStopEventKind, int, dict[str, Any]]]


def reference() -> Facts:
    return list(SCENARIO)


def reorders_rows() -> Facts:
    facts = reference()
    facts[1], facts[2] = facts[2], facts[1]
    return facts


def drops_a_row() -> Facts:
    return [f for i, f in enumerate(reference()) if i != 2]


def moves_a_time_across_a_decision_boundary() -> Facts:
    facts = reference()
    kind, _, extra = facts[3]
    facts[3] = (
        kind,
        2,
        extra,
    )  # a reading that moved before the countermeasure it reads
    return facts


BINDINGS: dict[str, Callable[[], Facts]] = {
    "reference": reference,
    "reorders_rows": reorders_rows,
    "drops_a_row": drops_a_row,
    "moves_a_time": moves_a_time_across_a_decision_boundary,
}


def run_queue(profile_id: str, binding_id: str, base: Path) -> ModelScenarioRun:
    state_dir = base / profile_id
    opened = emit(state_dir)
    lines: list[str] = []
    for kind, minutes, extra in BINDINGS[binding_id]():
        result = send(state_dir, stop_event(opened.id, kind, minutes, **extra))
        lines.append(f"{kind.value}@{minutes}: {result.status} {result.reason}".strip())
    stops = open_stops(state_dir).stops
    return ModelScenarioRun(
        profile_id=profile_id,
        binding=profile_id,
        proof_eligible=True,
        verdicts=("PASS",),
        lines=tuple(lines),
        rows=tuple(
            ModelScenarioRow(
                key="stop",
                content=json.dumps(
                    s.model_dump(mode="json", exclude={"id"}), sort_keys=True
                ),
            )
            for s in stops
        ),
        counts={"open_stops": len(stops), "decisions": len(decision_rows(state_dir))},
    )


def overlay(lab_binding: str) -> ModelFactoryBindingsOverlay:
    return ModelFactoryBindingsOverlay.model_validate(
        {
            "profiles": [{"id": "local", "baseline": True}, {"id": "lab"}],
            "bindings": {
                "local": dict.fromkeys(CODES, "reference"),
                "lab": dict.fromkeys(CODES, lab_binding),
            },
            "proof_bindings": ["reference"],
        }
    )


def parity(lab_binding: str, base: Path) -> Any:
    chosen = overlay(lab_binding)
    runs = tuple(
        run_queue(profile.id, chosen.binding_id(profile.id, "LED"), base)
        for profile in chosen.profiles
    )
    return compare_runs(runs)


def test_ac9_the_stop_projects_the_same_under_the_local_and_the_lab_profile(
    tmp_path: Path,
) -> None:
    assert parity("reference", tmp_path).passed
    baseline = run_queue("local", "reference", tmp_path / "again")
    assert baseline.counts == {"open_stops": 0, "decisions": 0}


@pytest.mark.parametrize("binding", ["reorders_rows", "drops_a_row", "moves_a_time"])
def test_ac9_a_stub_that_reorders_drops_or_moves_a_time_fails_the_comparison(
    binding: str, tmp_path: Path
) -> None:
    result = parity(binding, tmp_path)
    assert not result.passed
    assert result.first_difference


def test_ac9_the_queue_declares_its_row_of_the_protocol_table() -> None:
    import yaml

    contract = yaml.safe_load(
        (Path(handler.__file__).parent / "contract.yaml").read_text(encoding="utf-8")
    )
    declared = [d["name"] for d in contract["protocol_dependencies"]]
    assert sorted(declared) == sorted(CODES)
    assert all(d["required"] for d in contract["protocol_dependencies"])
