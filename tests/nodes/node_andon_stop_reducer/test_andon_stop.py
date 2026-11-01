# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Andon stop reducer: lifecycle, readbacks, leases, the gate, the contract and its configuration.

Every scope key, lane and class below is an invented fixture name or a class id of the
reducer's own registry.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.validation.validator_fsm_analysis import analyze_fsm

import omnimarket.handlers.rules_stop_gate as rule_module
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.rules_stop_gate import (
    fold_stop_event,
    load_andon_stop_contract,
    stop_gate,
)
from omnimarket.models.stop_gate import (
    EnumFoldStatus,
    EnumReadingOrigin,
    EnumReadingVerdict,
    EnumStopEventKind,
    EnumStopGateOutcome,
    EnumStopGateReason,
    EnumStopMode,
    ModelAndonStopConfig,
    ModelHandFixEvidence,
    ModelStopEvent,
    ModelStopFoldRequest,
    ModelStopGateRequest,
    ModelStopPolicy,
    ModelStopReading,
    ModelStopRecord,
    ModelStopScope,
)
from omnimarket.nodes.node_andon_stop_reducer import HandlerAndonStop
from omnimarket.nodes.node_andon_stop_reducer.overlay import load_stop_policy

pytestmark = pytest.mark.unit

NODE_DIR = Path(rule_module.__file__).parents[1] / "nodes/node_andon_stop_reducer"
FRAGMENT = Path(__file__).parents[2] / "fixtures/andon_stop/state_machine_fragment.yaml"
T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
OPEN, SWARMING, FIXED, STALE, CLEARED, DECLARED_FALSE = (
    "open",
    "swarming",
    "fixed",
    "stale",
    "cleared",
    "declared_false",
)
CLASS = "engine-policy-breach"
SCOPE = ModelStopScope(kind="engine", key="example-engine")
FSM, CONFIG = load_andon_stop_contract()
PASS = ModelStopReading(
    verdict=EnumReadingVerdict.PASS,
    proof_eligible=True,
    origin=EnumReadingOrigin.EXECUTED,
    covers_scope=True,
)
ROLES = {
    EnumStopEventKind.LEASE_GRANTED: "swarm_lane",
    EnumStopEventKind.LEASE_EXPIRED: "detector",
    EnumStopEventKind.STALE_LIMIT_REACHED: "detector",
    EnumStopEventKind.COUNTERMEASURE_MERGED: "swarm_lane",
    EnumStopEventKind.HAND_FIX_RECORDED: "operator",
    EnumStopEventKind.REPLACEMENTS_EXTENDED: "operator",
    EnumStopEventKind.READBACK: "readback",
    EnumStopEventKind.DECLARED_FALSE: "operator",
}


def stop(state: str = OPEN, **fields: Any) -> ModelStopRecord:
    return ModelStopRecord(
        id="stop-1",
        cls=CLASS,
        scope=SCOPE,
        state=state,
        stopped_at=fields.pop("stopped_at", T0),
        **fields,
    )


def event(
    kind: EnumStopEventKind, minutes: int = 0, role: str | None = None, **fields: Any
) -> ModelStopEvent:
    return ModelStopEvent(
        stop_id="stop-1",
        kind=kind,
        role=role if role is not None else ROLES[kind],
        at=T0 + timedelta(minutes=minutes),
        **fields,
    )


def fold(
    record: ModelStopRecord, ev: ModelStopEvent, config: ModelAndonStopConfig = CONFIG
) -> Any:
    return fold_stop_event(record, ev, FSM, config)


def with_consecutive(count: int) -> ModelAndonStopConfig:
    return CONFIG.model_copy(update={"consecutive_fixed_readings": count})


def readings_until_cleared(config: ModelAndonStopConfig, limit: int = 6) -> int:
    record = stop(FIXED, fixed_at=T0)
    for number in range(1, limit + 1):
        result = fold(
            record, event(EnumStopEventKind.READBACK, number, reading=PASS), config
        )
        assert result.status is EnumFoldStatus.APPLIED
        record = result.stop
        if record.state == CLEARED:
            assert record.cleared_at == T0 + timedelta(minutes=number)
            return number
    return -1


def policy(mode: EnumStopMode | None) -> ModelStopPolicy:
    return ModelStopPolicy(modes={} if mode is None else {CLASS: mode})


def gate(
    stops: tuple[ModelStopRecord, ...],
    mode: EnumStopMode | None = EnumStopMode.ENFORCE,
    **fields: Any,
) -> Any:
    return stop_gate(
        ModelStopGateRequest(
            scope=SCOPE,
            stops=stops,
            policy=policy(mode),
            now=T0 + timedelta(minutes=10),
            **fields,
        ),
        FSM,
    )


# AC1: the gate refuses naming the stop and its lane while open, and admits after cleared_at


@pytest.mark.parametrize("state", [OPEN, SWARMING, FIXED, STALE])
def test_ac1_gate_refuses_naming_the_stop_and_its_swarm_lane_while_open(
    state: str,
) -> None:
    record = stop(state, swarm_lane="swarm-lane-1")
    result = gate((record,))
    assert result.outcome is EnumStopGateOutcome.REFUSE
    assert result.stop_id == "stop-1"
    assert result.swarm_lane == "swarm-lane-1"
    assert "stop-1" in result.line
    assert "swarm-lane-1" in result.line


def test_ac1_gate_admits_after_cleared_at() -> None:
    cleared = stop(CLEARED, cleared_at=T0 + timedelta(minutes=5))
    result = gate((cleared,))
    assert result.outcome is EnumStopGateOutcome.ADMIT
    assert result.reason is EnumStopGateReason.NO_STOP


def test_ac1_gate_admits_a_declared_false_stop_and_another_scope() -> None:
    assert gate((stop(DECLARED_FALSE),)).outcome is EnumStopGateOutcome.ADMIT
    result = stop_gate(
        ModelStopGateRequest(
            scope=ModelStopScope(kind="engine", key="another-engine"),
            stops=(stop(),),
            policy=policy(EnumStopMode.ENFORCE),
            now=T0,
        ),
        FSM,
    )
    assert result.outcome is EnumStopGateOutcome.ADMIT


def test_ac1_full_lifecycle_refuses_until_the_clear_and_admits_after() -> None:
    record = stop()
    steps = [
        event(EnumStopEventKind.LEASE_GRANTED, 1, lane_id="swarm-lane-1"),
        event(EnumStopEventKind.COUNTERMEASURE_MERGED, 2),
        event(EnumStopEventKind.READBACK, 3, reading=PASS),
    ]
    for step in steps:
        assert gate((record,)).outcome is EnumStopGateOutcome.REFUSE
        record = fold(record, step).stop
    assert record.state == FIXED
    assert gate((record,)).outcome is EnumStopGateOutcome.REFUSE
    record = fold(record, event(EnumStopEventKind.READBACK, 4, reading=PASS)).stop
    assert record.state == CLEARED
    assert record.cleared_at == T0 + timedelta(minutes=4)
    assert gate((record,)).outcome is EnumStopGateOutcome.ADMIT


def test_the_swarm_lane_holding_a_live_lease_is_never_refused_by_its_own_stop() -> None:
    record = fold(
        stop(), event(EnumStopEventKind.LEASE_GRANTED, 1, lane_id="swarm-lane-1")
    ).stop
    own = gate((record,), lane_id="swarm-lane-1")
    other = gate((record,), lane_id="forged-lane")
    assert own.outcome is EnumStopGateOutcome.ADMIT
    assert own.reason is EnumStopGateReason.SWARM_LANE
    assert other.outcome is EnumStopGateOutcome.REFUSE


def test_an_expired_lease_no_longer_exempts_the_swarm_lane() -> None:
    record = stop(
        SWARMING,
        swarm_lane="swarm-lane-1",
        lease_expires_at=T0 + timedelta(minutes=5),
    )
    assert gate((record,), lane_id="swarm-lane-1").outcome is EnumStopGateOutcome.REFUSE


# AC2: an unreadable reading holds the stop


@pytest.mark.parametrize(
    "reading",
    [
        ModelStopReading(verdict=EnumReadingVerdict.UNKNOWN),
        ModelStopReading(verdict=EnumReadingVerdict.FAIL, covers_scope=True),
        ModelStopReading(
            verdict=EnumReadingVerdict.PASS,
            proof_eligible=False,
            origin=EnumReadingOrigin.EXECUTED,
            covers_scope=True,
        ),
        ModelStopReading(
            verdict=EnumReadingVerdict.PASS,
            proof_eligible=True,
            origin=EnumReadingOrigin.CACHED,
            covers_scope=True,
        ),
        ModelStopReading(
            verdict=EnumReadingVerdict.PASS,
            proof_eligible=True,
            origin=EnumReadingOrigin.EXECUTED,
            covers_scope=False,
        ),
    ],
)
def test_ac2_a_reading_that_does_not_count_leaves_the_stop_fixed_and_open(
    reading: ModelStopReading,
) -> None:
    record = stop(FIXED, fixed_at=T0, fixed_readings_seen=1)
    result = fold(record, event(EnumStopEventKind.READBACK, 1, reading=reading))
    assert result.status is EnumFoldStatus.APPLIED
    assert result.trigger == "readback_unknown"
    assert result.stop.state == FIXED
    assert result.stop.cleared_at is None
    assert result.stop.fixed_readings_seen == 0
    assert gate((result.stop,)).outcome is EnumStopGateOutcome.REFUSE


def test_ac2_an_unknown_reading_between_two_fixed_readings_breaks_the_run() -> None:
    record = stop(FIXED, fixed_at=T0)
    record = fold(record, event(EnumStopEventKind.READBACK, 1, reading=PASS)).stop
    unknown = ModelStopReading(verdict=EnumReadingVerdict.UNKNOWN)
    record = fold(record, event(EnumStopEventKind.READBACK, 2, reading=unknown)).stop
    record = fold(record, event(EnumStopEventKind.READBACK, 3, reading=PASS)).stop
    assert record.state == FIXED


def test_a_missing_reading_for_the_stopped_scope_reopens_the_stop_whatever_its_binding() -> (
    None
):
    missing = ModelStopReading(verdict=EnumReadingVerdict.MISSING, covers_scope=True)
    record = stop(FIXED, fixed_at=T0, swarm_lane="swarm-lane-1")
    result = fold(record, event(EnumStopEventKind.READBACK, 1, reading=missing))
    assert result.trigger == "readback_mismatch"
    assert result.stop.state == OPEN
    assert result.stop.swarm_lane is None


# AC3: the contract's state machine equals the fragment of section 5.8


def contract() -> dict[str, Any]:
    document = yaml.safe_load((NODE_DIR / "contract.yaml").read_text("utf-8"))
    assert isinstance(document, dict)
    return document


def test_ac3_state_machine_equals_the_fixture_copy_of_the_fragment() -> None:
    fragment = yaml.safe_load(FRAGMENT.read_text("utf-8"))
    assert contract()["state_machine"] == fragment["state_machine"]


def test_ac3_state_machine_loads_and_passes_analyze_fsm_with_no_finding() -> None:
    fsm = ModelFSMSubcontract.model_validate(contract()["state_machine"])
    result = analyze_fsm(fsm)
    assert not result.unreachable_states
    assert not result.dead_transitions
    assert not result.missing_transitions
    assert not result.cycles_without_exit
    assert not result.ambiguous_transitions
    assert not result.duplicate_state_names
    assert not result.errors


# AC6: a class in observe mode admits with the stop named


def test_ac6_observe_mode_admits_with_the_stop_named_never_refuses() -> None:
    for state in (OPEN, SWARMING, STALE):
        result = gate((stop(state, swarm_lane="swarm-lane-1"),), EnumStopMode.OBSERVE)
        assert result.outcome is EnumStopGateOutcome.ADMIT
        assert result.reason is EnumStopGateReason.OBSERVED
        assert result.stop_id == "stop-1"
        assert "stop-1" in result.line
        assert "observe" in result.line


# AC7: leases


def leased(replacements_used: int = 0, **fields: Any) -> ModelStopRecord:
    return stop(
        SWARMING,
        swarm_lane="swarm-lane-1",
        lease_expires_at=T0 + timedelta(minutes=CONFIG.lease_minutes),
        lease_replacements_used=replacements_used,
        **fields,
    )


def expiry(minutes_after: int = 1, lane: str = "swarm-lane-2") -> ModelStopEvent:
    return event(
        EnumStopEventKind.LEASE_EXPIRED,
        CONFIG.lease_minutes + minutes_after,
        lane_id=lane,
    )


def test_ac7_a_lease_past_its_expiry_is_replaceable() -> None:
    result = fold(leased(), expiry())
    assert result.status is EnumFoldStatus.APPLIED
    assert result.stop.state == SWARMING
    assert result.stop.swarm_lane == "swarm-lane-2"
    assert result.stop.lease_replacements_used == 1
    assert result.stop.lease_expires_at == expiry().at + timedelta(
        minutes=CONFIG.lease_minutes
    )
    assert result.actions == ("emit_lease_record",)


def test_ac7_a_lease_not_yet_expired_is_not_replaceable() -> None:
    early = event(EnumStopEventKind.LEASE_EXPIRED, 5, lane_id="swarm-lane-2")
    result = fold(leased(), early)
    assert result.status is EnumFoldStatus.REFUSED
    assert result.reason == "lease_not_expired"


def test_ac7_replacements_up_to_the_maximum_write_no_decision_row() -> None:
    record = leased()
    for used in range(CONFIG.max_lease_replacements):
        step = event(
            EnumStopEventKind.LEASE_EXPIRED,
            (used + 1) * (CONFIG.lease_minutes + 1),
            lane_id=f"swarm-lane-{used + 2}",
        )
        result = fold(record, step)
        assert result.stop.state == SWARMING
        assert "emit_decision_row" not in result.actions
        record = result.stop
    assert record.lease_replacements_used == CONFIG.max_lease_replacements


def test_ac7_a_replacement_past_the_maximum_writes_one_decision_row() -> None:
    record = leased(replacements_used=CONFIG.max_lease_replacements)
    result = fold(record, expiry())
    assert result.stop.state == STALE
    assert result.actions == ("emit_decision_row",)
    assert result.stop.swarm_lane is None


def test_ac7_the_maximum_comes_from_the_contract_config() -> None:
    one = CONFIG.model_copy(update={"max_lease_replacements": 1})
    assert fold(leased(replacements_used=0), expiry(), one).stop.state == SWARMING
    assert fold(leased(replacements_used=1), expiry(), one).stop.state == STALE


def test_a_stale_stop_never_clears_itself_and_leaves_only_by_lease_fix_or_false() -> (
    None
):
    record = stop(STALE)
    for reading_event in (
        event(EnumStopEventKind.READBACK, 1, reading=PASS),
        event(
            EnumStopEventKind.LEASE_GRANTED, 1, lane_id="swarm-lane-1", role="detector"
        ),
    ):
        assert fold(record, reading_event).status is EnumFoldStatus.REFUSED
    assert (
        fold(record, event(EnumStopEventKind.COUNTERMEASURE_MERGED, 1)).stop.state
        == FIXED
    )
    assert (
        fold(record, event(EnumStopEventKind.DECLARED_FALSE, 1)).stop.state
        == DECLARED_FALSE
    )


def test_a_stale_stop_takes_a_new_lease_only_after_the_operator_extends_the_replacements() -> (
    None
):
    exhausted = stop(STALE, lease_replacements_used=CONFIG.max_lease_replacements)
    grant = event(EnumStopEventKind.LEASE_GRANTED, 1, lane_id="swarm-lane-3")
    assert fold(exhausted, grant).status is EnumFoldStatus.REFUSED
    extended = fold(
        exhausted, event(EnumStopEventKind.REPLACEMENTS_EXTENDED, 1, extension=2)
    )
    assert extended.actions == ("emit_decision_row",)
    result = fold(extended.stop, grant)
    assert result.stop.state == SWARMING
    assert result.stop.swarm_lane == "swarm-lane-3"


def test_the_stale_limit_needs_the_configured_hours_and_no_live_lease() -> None:
    hours = CONFIG.stale_after_hours
    early = event(EnumStopEventKind.STALE_LIMIT_REACHED, hours * 60 - 1)
    due = event(EnumStopEventKind.STALE_LIMIT_REACHED, hours * 60)
    assert fold(stop(), early).status is EnumFoldStatus.REFUSED
    result = fold(stop(), due)
    assert result.stop.state == STALE
    assert result.actions == ("emit_decision_row",)
    live = leased()
    assert fold(live, due).status is EnumFoldStatus.REFUSED


def test_an_operator_declares_a_stop_false_from_every_open_state_with_one_decision_row() -> (
    None
):
    for state in (
        OPEN,
        SWARMING,
        FIXED,
        STALE,
    ):
        result = fold(stop(state), event(EnumStopEventKind.DECLARED_FALSE, 1))
        assert result.stop.state == DECLARED_FALSE
        assert result.actions == ("emit_decision_row",)
    terminal = fold(stop(CLEARED), event(EnumStopEventKind.DECLARED_FALSE, 1))
    assert terminal.status is EnumFoldStatus.REFUSED


def test_a_hand_fix_needs_the_failure_record_closed_by_hand_with_repair_evidence() -> (
    None
):
    good = ModelHandFixEvidence(
        closed_fixed_by_hand=True,
        first_occurrence=True,
        repair_evidence="merged change",
    )
    for bad in (
        None,
        good.model_copy(update={"first_occurrence": False}),
        good.model_copy(update={"closed_fixed_by_hand": False}),
        good.model_copy(update={"repair_evidence": " "}),
    ):
        refused = fold(
            stop(), event(EnumStopEventKind.HAND_FIX_RECORDED, 1, hand_fix=bad)
        )
        assert refused.status is EnumFoldStatus.REFUSED
    ok = fold(stop(), event(EnumStopEventKind.HAND_FIX_RECORDED, 1, hand_fix=good))
    assert ok.stop.state == FIXED


def test_a_countermeasure_that_does_not_fix_reopens_the_stop() -> None:
    record = stop(FIXED, fixed_at=T0, fixed_readings_seen=1)
    missing = ModelStopReading(verdict=EnumReadingVerdict.MISSING, covers_scope=True)
    assert (
        fold(record, event(EnumStopEventKind.READBACK, 1, reading=missing)).stop.state
        == OPEN
    )


# AC8: configuration


def test_ac8_the_clear_moves_from_the_first_readback_to_the_third_with_the_configured_count() -> (
    None
):
    assert readings_until_cleared(with_consecutive(1)) == 1
    assert readings_until_cleared(with_consecutive(2)) == 2
    assert readings_until_cleared(with_consecutive(3)) == 3


def test_ac8_the_committed_config_values_and_registry_are_the_plan_register() -> None:
    config = contract()["config"]["andon_stop"]
    numbers = {k: v for k, v in config.items() if isinstance(v, int)}
    assert numbers == {
        "stale_after_hours": 4,
        "storm_distinct_keys": 5,
        "lease_minutes": 90,
        "max_lease_replacements": 3,
        "consecutive_fixed_readings": 2,
        "canary_dispatches_per_readback_interval": 1,
        "recovery_minimum_crossings": 1,
    }
    assert [c["id"] for c in config["stop_classes"]] == [
        "engine-policy-breach",
        "repository-without-lander",
        "host-bound-ruling-drifted",
        "ruling-drifted",
        "seam-mismatch",
    ]


def test_ac8_the_handler_reads_its_numbers_from_the_contract_it_is_given() -> None:
    handler = HandlerAndonStop(config=with_consecutive(1))
    result = handler.handle(
        ModelStopFoldRequest(
            stop=stop(FIXED, fixed_at=T0),
            event=event(EnumStopEventKind.READBACK, 1, reading=PASS),
        )
    )
    assert result.stop.state == CLEARED
    default = HandlerAndonStop().handle(
        ModelStopFoldRequest(
            stop=stop(FIXED, fixed_at=T0),
            event=event(EnumStopEventKind.READBACK, 1, reading=PASS),
        )
    )
    assert default.stop.state == FIXED


def test_ac8_an_absent_overlay_mode_refuses_naming_the_key() -> None:
    result = gate((stop(),), mode=None)
    assert result.outcome is EnumStopGateOutcome.REFUSE
    assert result.reason is EnumStopGateReason.MODE_MISSING
    assert f"andon_stop.modes.{CLASS}" in result.line


def test_ac8_an_absent_overlay_refuses_naming_the_keys(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError, match="andon_stop"):
        load_stop_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    (tmp_path / "andon_stop").mkdir()
    (tmp_path / "andon_stop/overlay.yaml").write_text("modes: {a-class: observe}\n")
    with pytest.raises(NodeOverlayError, match="enforce_citations"):
        load_stop_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_ac8_the_overlay_supplies_the_mode_and_an_enforced_class_needs_its_citation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "andon_stop"
    root.mkdir()
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    (root / "overlay.yaml").write_text(
        f"modes: {{{CLASS}: enforce}}\nenforce_citations: {{}}\n"
    )
    with pytest.raises(NodeOverlayError, match=f"enforce_citations.{CLASS}"):
        load_stop_policy(environ)
    (root / "overlay.yaml").write_text(
        f"modes: {{{CLASS}: enforce}}\nenforce_citations: {{{CLASS}: 'a cited change'}}\n"
    )
    loaded = load_stop_policy(environ)
    assert loaded.modes[CLASS] is EnumStopMode.ENFORCE
    assert gate((stop(),), mode=None).outcome is EnumStopGateOutcome.REFUSE


def test_ac8_no_number_of_the_register_is_a_literal_in_the_rule_or_the_handlers() -> (
    None
):
    register = {4, 5, 90, 3}
    sources = [
        Path(rule_module.__file__),
        *(NODE_DIR / "handlers").glob("*.py"),
    ]
    found: list[str] = []
    for source in sources:
        for node in ast.walk(ast.parse(source.read_text("utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and type(node.value) is int
                and node.value in register
            ):
                found.append(f"{source.name}:{node.lineno}={node.value}")
    assert found == []


def test_the_registry_refuses_a_repeated_id_or_label_and_a_missing_writer_role() -> (
    None
):
    data = CONFIG.model_dump(mode="json")
    repeated = {
        **data,
        "stop_classes": [*data["stop_classes"], data["stop_classes"][0]],
    }
    with pytest.raises(ValueError, match="repeats id"):
        ModelAndonStopConfig.model_validate(repeated)
    relabelled = {
        **data,
        "stop_classes": [
            *data["stop_classes"],
            {**data["stop_classes"][0], "id": "another-class"},
        ],
    }
    with pytest.raises(ValueError, match="repeats label"):
        ModelAndonStopConfig.model_validate(relabelled)
    roles = dict(data["writer_roles"])
    del roles["readback"]
    with pytest.raises(ValueError, match="writer_roles"):
        ModelAndonStopConfig.model_validate({**data, "writer_roles": roles})


def test_the_contract_declares_no_protocol_dependency_and_is_pure() -> None:
    document = contract()
    assert "protocol_dependencies" not in document
    assert document["descriptor"]["purity"] == "pure"
