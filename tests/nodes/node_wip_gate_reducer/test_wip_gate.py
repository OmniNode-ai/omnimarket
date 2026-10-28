# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""WIP gate reducer: reader, gate, override, exemption and admission audit.

Every repository name below is an invented fixture name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.validation.validator_fsm_analysis import analyze_fsm

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.rules_wip_gate import (
    audit_admissions,
    derive_limit,
    evaluate_wip_gate,
    load_wip_gate_contract,
    read_wip,
)
from omnimarket.models.wip_gate import (
    EnumWipEvidenceWriter,
    EnumWipExemptionKind,
    EnumWipOutcome,
    EnumWipReason,
    ModelWipAuditRequest,
    ModelWipClaimRow,
    ModelWipExemption,
    ModelWipGateConfig,
    ModelWipGateRequest,
    ModelWipLimit,
    ModelWipOverrideEvidence,
    ModelWipPolicy,
    ModelWipReading,
    ModelWipReadRequest,
    ModelWipStateOutcome,
)
from omnimarket.nodes.node_wip_gate_reducer.handlers.handler_wip_gate import (
    HandlerWipGate,
)
from omnimarket.nodes.node_wip_gate_reducer.overlay import (
    ModelWipLimitsOverlay,
    load_wip_policy,
)

NODE_DIR = Path(__file__).parents[3] / "src/omnimarket/nodes/node_wip_gate_reducer"
FRAGMENT = Path(__file__).parents[2] / "fixtures/wip_gate/state_machine_fragment.yaml"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
REPO_A, REPO_B = "fixture-repo-a", "fixture-repo-b"
SWARM_LANE = "fixture-swarm-lane-1"
DISPATCH = "dispatch-1"

FSM, CONFIG = load_wip_gate_contract()


def _limit(limit: int = 5, mph: float = 1.5) -> ModelWipLimit:
    return ModelWipLimit(
        limit=limit,
        merges_per_hour=mph,
        method="first-parent merges over the trailing window",
        evidence="fixture measurement",
    )


def _policy(enforce: str = "fixture ruling", limit: int = 5) -> ModelWipPolicy:
    return ModelWipPolicy(
        enforce_ruling=enforce,
        dispatchable_repos=(REPO_A, REPO_B),
        limits={REPO_A: _limit(limit), REPO_B: _limit(3, 1.0)},
        exempt_roles=("fixture-cleanup",),
    )


def _state(
    open_n: int = 9,
    drafts: int = 3,
    *,
    repo: str = REPO_A,
    read_at: datetime = NOW,
    queued: int = 0,
) -> dict[str, Any]:
    prs: dict[str, Any] = {}
    for i in range(open_n):
        prs[f"{repo}#{i + 1}"] = {
            "queued": i >= open_n - queued,
            "facts": {
                "repo": repo,
                "number": i + 1,
                "state": "OPEN",
                "draft": i < drafts,
                "merged_at": "",
            },
        }
    prs[f"{repo}#900"] = {
        "queued": False,
        "facts": {
            "repo": repo,
            "number": 900,
            "state": "MERGED",
            "draft": False,
            "merged_at": "2026-10-01T00:00:00Z",
        },
    }
    return {"prs": prs, "last_tick": read_at.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _request(**over: Any) -> ModelWipGateRequest:
    base: dict[str, Any] = {
        "repo": REPO_A,
        "dispatch_id": DISPATCH,
        "now": NOW,
        "state": _state(),
        "policy": _policy(),
        "config": CONFIG,
    }
    base.update(over)
    return ModelWipGateRequest(**base)


def _evidence(**over: Any) -> ModelWipOverrideEvidence:
    base: dict[str, Any] = {
        "slot_id": "slot-1",
        "slot_generation": 4,
        "evidence_key": "override:slot-1:4",
        "repo": REPO_A,
        "dispatch_id": DISPATCH,
        "decision_id": "decision-1",
        "written_by": EnumWipEvidenceWriter.SLOT_REDUCER,
        "written_at": NOW - timedelta(minutes=2),
    }
    base.update(over)
    return ModelWipOverrideEvidence(**base)


def _exemption(**over: Any) -> ModelWipExemption:
    base: dict[str, Any] = {
        "kind": EnumWipExemptionKind.SWARM_LEASE,
        "repo": REPO_A,
        "lane_id": SWARM_LANE,
        "role": "fixture-cleanup",
        "written_by": EnumWipEvidenceWriter.DISPATCHER,
        "expires_at": NOW + timedelta(minutes=30),
    }
    base.update(over)
    return ModelWipExemption(**base)


def _read(**over: Any) -> list[str]:
    base: dict[str, Any] = {
        "now": NOW,
        "state": _state(),
        "policy": _policy(),
        "config": CONFIG,
    }
    base.update(over)
    return list(read_wip(ModelWipReadRequest(**base)).lines)


# AC1, AC2: the reader


def test_ac1_reader_counts_drafts_and_prints_both_counts() -> None:
    lines = _read()
    assert lines[0] == f"WIP {REPO_A} open=9 drafts=3 limit=5 exempt=0"
    assert f"MISSING wip:{REPO_A}" in lines


def test_ac1_merged_and_queued_pull_requests_are_not_counted() -> None:
    line = _read(state=_state(open_n=4, drafts=1, queued=1))[0]
    assert line.startswith(f"WIP {REPO_A} open=3 drafts=1 ")


def test_ac1_no_missing_line_under_the_limit() -> None:
    lines = _read(state=_state(open_n=4, drafts=0))
    assert not any(line.startswith("MISSING") for line in lines)


def test_ac1_no_missing_line_when_only_drafts_wait() -> None:
    lines = _read(state=_state(open_n=6, drafts=6))
    assert lines[0] == f"WIP {REPO_A} open=6 drafts=6 limit=5 exempt=0"
    assert not any(line.startswith("MISSING") for line in lines)


def test_ac2_line_prints_exempt_count_of_valid_exemptions_only() -> None:
    forged = _exemption(written_by=EnumWipEvidenceWriter.SLOT_REDUCER)
    lines = _read(
        exemptions=(_exemption(), forged),
        authorized_swarm_lanes=(SWARM_LANE,),
    )
    assert lines[0] == f"WIP {REPO_A} open=9 drafts=3 limit=5 exempt=1"


# AC6: unreadable never reads 0


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (None, "missing_reading"),
        ({"prs": "garbage", "last_tick": "2026-10-27T12:00:00Z"}, "unreadable_state"),
        ({"prs": {}}, "unreadable_state"),
        (
            {"prs": {"x#1": {"facts": []}}, "last_tick": "2026-10-27T12:00:00Z"},
            "unreadable_state",
        ),
    ],
)
def test_ac6_unreadable_state_prints_unknown_never_zero(
    state: dict[str, Any] | None, reason: str
) -> None:
    lines = _read(state=state)
    assert lines[0] == f"UNKNOWN wip:{REPO_A} reason={reason}"
    assert not any("open=0" in line for line in lines)


def test_ac6_a_readable_empty_repo_is_a_real_zero() -> None:
    state = _state(open_n=0)
    assert _read(state=state)[0] == f"WIP {REPO_A} open=0 drafts=0 limit=5 exempt=0"


# The gate: 9 open and limit 5


def test_nine_open_limit_five_enforcing_refuses() -> None:
    decision = evaluate_wip_gate(_request(), FSM)
    assert decision.outcome is EnumWipOutcome.REFUSE
    assert decision.fsm_state == "refused"
    assert decision.reason is EnumWipReason.AT_LIMIT
    assert (decision.open, decision.drafts, decision.limit) == (9, 3, 5)


def test_nine_open_limit_five_not_enforcing_warns() -> None:
    decision = evaluate_wip_gate(_request(policy=_policy(enforce="")), FSM)
    assert decision.outcome is EnumWipOutcome.WARN
    assert decision.fsm_state == "warned"


def test_under_limit_admits() -> None:
    decision = evaluate_wip_gate(_request(state=_state(open_n=4, drafts=0)), FSM)
    assert (decision.outcome, decision.fsm_state) == (EnumWipOutcome.ADMIT, "admitted")


def test_the_handler_gives_the_rule_answer() -> None:
    request = _request()
    assert HandlerWipGate().handle(request) == evaluate_wip_gate(request, FSM)


# AC5: enforcement and failure-closed readings


def _stale_state() -> dict[str, Any]:
    return _state(read_at=NOW - timedelta(minutes=11))


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (_stale_state(), EnumWipReason.STALE_READING),
        ({"prs": 3, "last_tick": "x"}, EnumWipReason.UNREADABLE_STATE),
        (None, EnumWipReason.MISSING_READING),
    ],
)
def test_ac5_unusable_reading_refuses_when_enforcing_naming_the_reason(
    state: dict[str, Any] | None, reason: EnumWipReason
) -> None:
    decision = evaluate_wip_gate(_request(state=state), FSM)
    assert decision.outcome is EnumWipOutcome.REFUSE
    assert decision.reason is reason
    assert reason.value in decision.line


def test_ac5_missing_limit_refuses_when_enforcing_naming_the_reason() -> None:
    decision = evaluate_wip_gate(_request(repo="fixture-unlisted"), FSM)
    assert decision.outcome is EnumWipOutcome.REFUSE
    assert decision.reason is EnumWipReason.LIMIT_MISSING


@pytest.mark.parametrize(
    "state", [_stale_state(), {"prs": 3, "last_tick": "x"}, None, _state()]
)
def test_ac5_with_enforce_ruling_empty_the_gate_never_refuses(
    state: dict[str, Any] | None,
) -> None:
    for repo in (REPO_A, "fixture-unlisted"):
        decision = evaluate_wip_gate(
            _request(repo=repo, state=state, policy=_policy(enforce="")), FSM
        )
        assert decision.outcome is not EnumWipOutcome.REFUSE


def test_ac5_unusable_reading_without_enforcement_prints_unknown() -> None:
    decision = evaluate_wip_gate(_request(state=None, policy=_policy(enforce="")), FSM)
    assert decision.outcome is EnumWipOutcome.WARN
    assert decision.line.startswith(f"UNKNOWN wip:{REPO_A}")
    assert decision.open is None


# AC7: override evidence


def test_ac7_valid_override_admits_at_the_limit() -> None:
    decision = evaluate_wip_gate(_request(override=_evidence()), FSM)
    assert decision.outcome is EnumWipOutcome.ADMIT
    assert decision.fsm_state == "admitted_override"


def test_ac7_override_claimed_by_this_dispatch_is_still_valid() -> None:
    decision = evaluate_wip_gate(
        _request(override=_evidence(), override_consumed_by=DISPATCH), FSM
    )
    assert decision.fsm_state == "admitted_override"


@pytest.mark.parametrize(
    ("evidence", "consumed_by"),
    [
        pytest.param(_evidence(evidence_key="override:slot-1:5"), None, id="forged"),
        pytest.param(_evidence(repo=REPO_B), None, id="other-repository"),
        pytest.param(_evidence(dispatch_id="dispatch-2"), None, id="other-dispatch"),
        pytest.param(
            _evidence(written_at=NOW - timedelta(minutes=11)), None, id="expired"
        ),
        pytest.param(_evidence(), "dispatch-2", id="already-consumed"),
        pytest.param(
            _evidence(written_by=EnumWipEvidenceWriter.DISPATCHER),
            None,
            id="not-slot-reducer",
        ),
    ],
)
def test_ac7_invalid_override_refuses_at_the_limit(
    evidence: ModelWipOverrideEvidence, consumed_by: str | None
) -> None:
    decision = evaluate_wip_gate(
        _request(override=evidence, override_consumed_by=consumed_by), FSM
    )
    assert decision.outcome is EnumWipOutcome.REFUSE
    assert decision.reason is EnumWipReason.AT_LIMIT


def test_ac7_a_genuine_old_override_replayed_onto_a_second_brief_refuses() -> None:
    evidence = _evidence()
    first = evaluate_wip_gate(
        _request(override=evidence, override_consumed_by=DISPATCH), FSM
    )
    second = evaluate_wip_gate(
        _request(
            dispatch_id="dispatch-2", override=evidence, override_consumed_by=DISPATCH
        ),
        FSM,
    )
    assert first.fsm_state == "admitted_override"
    assert second.outcome is EnumWipOutcome.REFUSE


# AC8: override never bypasses the reading rule


@pytest.mark.parametrize("state", [_stale_state(), {"prs": 3, "last_tick": "x"}, None])
def test_ac8_valid_override_with_unusable_reading_refuses_or_warns(
    state: dict[str, Any] | None,
) -> None:
    enforcing = evaluate_wip_gate(_request(state=state, override=_evidence()), FSM)
    off = evaluate_wip_gate(
        _request(state=state, override=_evidence(), policy=_policy(enforce="")), FSM
    )
    assert enforcing.outcome is EnumWipOutcome.REFUSE
    assert off.outcome is EnumWipOutcome.WARN
    assert "admitted_override" not in (enforcing.fsm_state, off.fsm_state)


# Exemption on the gate


def test_exemption_admits_with_swarm_lease_and_open_pr_evidence() -> None:
    swarm = evaluate_wip_gate(
        _request(exemption=_exemption(), authorized_swarm_lanes=(SWARM_LANE,)), FSM
    )
    on_pr = evaluate_wip_gate(
        _request(exemption=_exemption(kind=EnumWipExemptionKind.OPEN_PR, pr_number=2)),
        FSM,
    )
    assert swarm.fsm_state == on_pr.fsm_state == "admitted_exempt"


def test_forged_exemption_does_not_admit() -> None:
    decision = evaluate_wip_gate(_request(exemption=_exemption()), FSM)
    assert decision.outcome is EnumWipOutcome.REFUSE


# AC9: the audit


def _claim(
    ts: datetime = NOW, *, lane: str = "fixture-lane", **over: Any
) -> ModelWipClaimRow:
    base: dict[str, Any] = {"lane": lane, "ts": ts, "repo": REPO_A, "opens_pr": True}
    base.update(over)
    return ModelWipClaimRow(**base)


def _readings(open_n: int = 9) -> tuple[ModelWipReading, ...]:
    return (
        ModelWipReading(
            repo=REPO_A,
            readable=True,
            read_at=NOW - timedelta(minutes=5),
            open=open_n,
            drafts=0,
            waiting=open_n,
        ),
    )


def _audit(claims: tuple[ModelWipClaimRow, ...], **over: Any) -> int:
    base: dict[str, Any] = {
        "claims": claims,
        "readings": _readings(),
        "policy": _policy(),
        "authorized_swarm_lanes": (SWARM_LANE,),
    }
    base.update(over)
    return audit_admissions(ModelWipAuditRequest(**base)).breaches


def test_ac9_a_claim_after_the_limit_reads_one() -> None:
    assert _audit((_claim(),)) == 1


def test_ac9_zero_cases() -> None:
    assert _audit(()) == 0
    assert _audit((_claim(),), readings=_readings(open_n=4)) == 0
    assert _audit((_claim(opens_pr=False),)) == 0
    assert _audit((_claim(),), policy=_policy(enforce="")) == 0


def test_ac9_claim_before_any_reading_is_unaudited_not_clean() -> None:
    result = audit_admissions(
        ModelWipAuditRequest(
            claims=(_claim(NOW - timedelta(hours=1)),),
            readings=_readings(),
            policy=_policy(),
        )
    )
    assert (result.breaches, result.unaudited) == (0, 1)


def test_ac9_wip_idle_override_is_not_a_breach() -> None:
    assert _audit((_claim(wip_idle_override=True),)) == 0


def test_ac9_valid_swarm_exemption_reads_zero() -> None:
    assert _audit((_claim(exemption=_exemption()),)) == 0


def test_ac9_valid_open_pr_exemption_reads_zero() -> None:
    claim = _claim(exemption=_exemption(kind=EnumWipExemptionKind.OPEN_PR, pr_number=2))
    assert _audit((claim,), state=_state()) == 0


def test_ac9_forged_exemption_reads_one() -> None:
    forged = _exemption(lane_id="fixture-not-named-by-the-stop")
    assert _audit((_claim(exemption=forged),)) == 1
    assert (
        _audit(
            (
                _claim(
                    exemption=_exemption(written_by=EnumWipEvidenceWriter.SLOT_REDUCER)
                ),
            )
        )
        == 1
    )


def test_ac9_expired_exemption_reads_one() -> None:
    expired = _exemption(expires_at=NOW - timedelta(minutes=1))
    assert _audit((_claim(exemption=expired),)) == 1


# AC3: the contract equals the plan fragment


def _contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text("utf-8"))


def test_ac3_state_machine_equals_the_fixture_copy_of_the_fragment() -> None:
    fragment = yaml.safe_load(FRAGMENT.read_text("utf-8"))
    assert _contract()["state_machine"] == fragment["state_machine"]


def test_ac3_state_machine_passes_analyze_fsm_with_no_finding() -> None:
    fsm = ModelFSMSubcontract.model_validate(_contract()["state_machine"])
    result = analyze_fsm(fsm)
    assert not result.unreachable_states
    assert not result.dead_transitions
    assert not result.missing_transitions
    assert not result.cycles_without_exit
    assert not result.ambiguous_transitions
    assert not result.duplicate_state_names
    assert not result.errors


# AC4: the overlay


def _overlay_doc() -> dict[str, Any]:
    return {
        "enforce_ruling": "fixture ruling",
        "dispatchable_repos": [REPO_A, REPO_B],
        "limits": {
            REPO_A: {
                "limit": 5,
                "merges_per_hour": 1.5,
                "method": "m",
                "evidence": "e",
            },
            REPO_B: {
                "limit": 3,
                "merges_per_hour": 1.0,
                "method": "m",
                "evidence": "e",
            },
        },
        "exempt_roles": ["fixture-cleanup"],
    }


def _write_overlay(root: Path, doc: dict[str, Any]) -> dict[str, str]:
    path = root / "wip_limits"
    path.mkdir(parents=True)
    (path / "overlay.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def test_ac4_every_dispatchable_repo_has_a_limit_with_evidence(tmp_path: Path) -> None:
    doc = _overlay_doc()
    policy = load_wip_policy(_write_overlay(tmp_path, doc))
    assert set(policy.dispatchable_repos) == set(policy.limits)
    assert all(entry.evidence for entry in policy.limits.values())
    assert all(
        entry.limit == derive_limit(entry.merges_per_hour, CONFIG)
        for entry in policy.limits.values()
    )


def test_ac4_missing_limit_entry_refuses_naming_the_key() -> None:
    doc = _overlay_doc()
    del doc["limits"][REPO_B]
    with pytest.raises(NodeOverlayError, match=rf"wip_limits\.limits\.{REPO_B}"):
        ModelWipLimitsOverlay.model_validate(doc).policy()


def test_ac4_missing_evidence_refuses_naming_the_field(tmp_path: Path) -> None:
    doc = _overlay_doc()
    del doc["limits"][REPO_A]["evidence"]
    with pytest.raises(NodeOverlayError, match=rf"limits\.{REPO_A}\.evidence"):
        load_wip_policy(_write_overlay(tmp_path, doc))


def test_ac4_missing_top_level_key_refuses_naming_it(tmp_path: Path) -> None:
    doc = _overlay_doc()
    del doc["enforce_ruling"]
    with pytest.raises(NodeOverlayError, match="enforce_ruling"):
        load_wip_policy(_write_overlay(tmp_path, doc))


# AC10: no names in the package


def test_ac10_package_names_no_fixture_repository_or_lane() -> None:
    fixture_names = (REPO_A, REPO_B, SWARM_LANE, "fixture-")
    sources = [
        *NODE_DIR.rglob("*.py"),
        *NODE_DIR.rglob("*.yaml"),
        NODE_DIR.parents[1] / "handlers/rules_wip_gate.py",
        NODE_DIR.parents[1] / "models/wip_gate.py",
    ]
    assert sources
    for path in sources:
        text = path.read_text("utf-8")
        for name in fixture_names:
            assert name not in text, f"{name} in {path}"


# AC11: configuration


def test_ac11_limit_comes_from_config_hours_of_landing() -> None:
    assert derive_limit(3, CONFIG) == 9
    assert derive_limit(3, CONFIG.model_copy(update={"hours_of_landing": 2})) == 6
    assert derive_limit(0.1, CONFIG) == CONFIG.limit_floor
    assert derive_limit(0.1, CONFIG.model_copy(update={"limit_floor": 7})) == 7


def test_ac11_committed_config_values() -> None:
    assert (
        ModelWipGateConfig(
            limit_floor=3,
            hours_of_landing=3,
            trailing_days=7,
            reading_max_age_minutes=10,
            override_evidence_minutes=10,
            state_outcomes={
                "admitted": ModelWipStateOutcome(
                    outcome=EnumWipOutcome.ADMIT, reason=EnumWipReason.UNDER_LIMIT
                ),
                "admitted_exempt": ModelWipStateOutcome(
                    outcome=EnumWipOutcome.ADMIT, reason=EnumWipReason.EXEMPT
                ),
                "admitted_override": ModelWipStateOutcome(
                    outcome=EnumWipOutcome.ADMIT, reason=EnumWipReason.OVERRIDE
                ),
                "warned": ModelWipStateOutcome(outcome=EnumWipOutcome.WARN),
                "refused": ModelWipStateOutcome(outcome=EnumWipOutcome.REFUSE),
            },
        )
        == CONFIG
    )


def test_ac11_reading_eleven_minutes_old_is_stale_under_10_and_fresh_under_15() -> None:
    state = _state(read_at=NOW - timedelta(minutes=11))
    strict = evaluate_wip_gate(_request(state=state), FSM)
    loose = evaluate_wip_gate(
        _request(
            state=state,
            config=CONFIG.model_copy(update={"reading_max_age_minutes": 15}),
        ),
        FSM,
    )
    assert strict.reason is EnumWipReason.STALE_READING
    assert loose.reason is EnumWipReason.AT_LIMIT


def test_ac11_override_window_comes_from_config() -> None:
    old = _evidence(written_at=NOW - timedelta(minutes=11))
    wide = CONFIG.model_copy(update={"override_evidence_minutes": 15})
    assert (
        evaluate_wip_gate(_request(override=old), FSM).outcome is EnumWipOutcome.REFUSE
    )
    assert (
        evaluate_wip_gate(_request(override=old, config=wide), FSM).fsm_state
        == "admitted_override"
    )


def test_ac11_limit_comes_from_the_overlay() -> None:
    state = _state(open_n=6, drafts=0)
    five = evaluate_wip_gate(_request(state=state), FSM)
    seven = evaluate_wip_gate(_request(state=state, policy=_policy(limit=7)), FSM)
    assert five.outcome is EnumWipOutcome.REFUSE
    assert seven.outcome is EnumWipOutcome.ADMIT


# AC12: bindings


def test_ac12_contract_declares_no_protocol_dependency() -> None:
    contract = _contract()
    assert "protocol_dependencies" not in contract
    assert "protocol_dependencies" not in (NODE_DIR / "metadata.yaml").read_text(
        "utf-8"
    )


def test_ac12_gate_is_deterministic_across_the_rule_and_the_handler() -> None:
    request = _request(override=_evidence())
    first = evaluate_wip_gate(request, FSM).model_dump_json()
    second = HandlerWipGate().handle(request).model_dump_json()
    assert first == second


def test_ac12_contract_fsm_loads_without_the_overlay_or_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    fsm, config = load_wip_gate_contract()
    assert fsm.initial_state == "requested"
    assert config.limit_floor == 3


def test_every_terminal_state_has_a_declared_outcome_in_the_contract() -> None:
    terminal = {state.state_name for state in FSM.states if state.is_terminal}
    assert set(CONFIG.state_outcomes) == terminal


def test_the_outcome_of_a_state_is_read_from_the_contract_config() -> None:
    request = _request(repo=REPO_A)
    before = evaluate_wip_gate(request, FSM)
    assert (before.fsm_state, before.outcome) == ("refused", EnumWipOutcome.REFUSE)
    moved = CONFIG.model_copy(
        update={
            "state_outcomes": {
                **CONFIG.state_outcomes,
                "refused": ModelWipStateOutcome(
                    outcome=EnumWipOutcome.WARN, reason=EnumWipReason.EXEMPT
                ),
            }
        }
    )
    after = evaluate_wip_gate(request.model_copy(update={"config": moved}), FSM)
    assert after.fsm_state == "refused"
    assert (after.outcome, after.reason) == (EnumWipOutcome.WARN, EnumWipReason.EXEMPT)
