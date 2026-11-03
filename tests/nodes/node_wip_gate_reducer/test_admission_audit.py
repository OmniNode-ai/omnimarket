# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The admission audit judges each PR-opening CLAIM against the witness written at dispatch.

Every repository, lane and dispatch name below is an invented fixture name.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers import rules_wip_gate
from omnimarket.handlers.rules_wip_gate import (
    audit_admissions,
    build_admission_witness,
    decode_witness_cell,
    encode_witness_cell,
    evaluate_wip_gate,
    load_wip_gate_contract,
    policy_digest,
)
from omnimarket.models.wip_gate import (
    EnumWipBreachReason,
    EnumWipEvidenceWriter,
    EnumWipExemptionKind,
    EnumWipOutcome,
    EnumWipOverrideReason,
    EnumWipReason,
    ModelWipAdmissionWitness,
    ModelWipAuditRequest,
    ModelWipClaimRow,
    ModelWipExemption,
    ModelWipGateConfig,
    ModelWipGateRequest,
    ModelWipLimit,
    ModelWipOverrideClaim,
    ModelWipOverrideEvidence,
    ModelWipPolicy,
    ModelWipReading,
    ModelWipSlotDecision,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
REPO_A, REPO_B = "fixture-repo-a", "fixture-repo-b"
SWARM_LANE = "fixture-swarm-lane-1"
DISPATCH, OTHER_DISPATCH = "dispatch-1", "dispatch-2"
RULING = "fixture ruling"

FSM, CONFIG = load_wip_gate_contract()


def _policy(enforce: str = RULING, limit: int = 5) -> ModelWipPolicy:
    return ModelWipPolicy(
        enforce_ruling=enforce,
        dispatchable_repos=(REPO_A, REPO_B),
        limits={
            REPO_A: ModelWipLimit(
                limit=limit,
                merges_per_hour=1.5,
                method="fixture method",
                evidence="fixture measurement",
            )
        },
        exempt_roles=("fixture-cleanup",),
    )


def _reading(**over: Any) -> ModelWipReading:
    base: dict[str, Any] = {
        "repo": REPO_A,
        "readable": True,
        "read_at": NOW - timedelta(minutes=5),
        "open": 9,
        "drafts": 0,
        "waiting": 9,
        "open_pr_numbers": (1, 2, 3, 4, 5, 6, 7, 8, 9),
    }
    base.update(over)
    return ModelWipReading(**base)


def _evidence(**over: Any) -> ModelWipOverrideEvidence:
    base: dict[str, Any] = {
        "slot_id": "slot-1",
        "slot_generation": 4,
        "evidence_key": "override:slot-1:4",
        "repo": REPO_A,
        "dispatch_id": DISPATCH,
        "decision_id": "decision-1",
        "reason": EnumWipOverrideReason.IDLE,
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


def _decision(**over: Any) -> ModelWipSlotDecision:
    base: dict[str, Any] = {
        "decision_id": "decision-1",
        "slot_id": "slot-1",
        "slot_generation": 4,
        "repo": REPO_A,
        "dispatch_id": DISPATCH,
        "reason": EnumWipOverrideReason.IDLE,
    }
    base.update(over)
    return ModelWipSlotDecision(**base)


def _override_claim(**over: Any) -> ModelWipOverrideClaim:
    base: dict[str, Any] = {
        "evidence_key": "override:slot-1:4",
        "slot_id": "slot-1",
        "slot_generation": 4,
        "repo": REPO_A,
        "dispatch_id": DISPATCH,
        "consumer": DISPATCH,
        "expires_at": NOW + timedelta(minutes=8),
    }
    base.update(over)
    return ModelWipOverrideClaim(**base)


def _witness(**over: Any) -> ModelWipAdmissionWitness:
    base: dict[str, Any] = {
        "repo": REPO_A,
        "dispatch_id": DISPATCH,
        "dispatched_at": NOW,
        "reading": _reading(),
        "limit": 5,
        "policy_digest": policy_digest(_policy()),
        "enforce_ruling": RULING,
        "exempt_roles": ("fixture-cleanup",),
    }
    base.update(over)
    return ModelWipAdmissionWitness(**base)


def _claim(**over: Any) -> ModelWipClaimRow:
    base: dict[str, Any] = {
        "lane": "fixture-lane",
        "ts": NOW + timedelta(seconds=30),
        "repo": REPO_A,
        "dispatch_id": DISPATCH,
        "opens_pr": True,
        "witness": _witness(),
    }
    base.update(over)
    return ModelWipClaimRow(**base)


def _override_claim_row(**witness_over: Any) -> ModelWipClaimRow:
    return _claim(witness=_witness(override=_evidence(), **witness_over))


def _audit(*claims: ModelWipClaimRow, **over: Any) -> int:
    return _audit_result(*claims, **over).breaches


def _audit_result(*claims: ModelWipClaimRow, **over: Any) -> Any:
    base: dict[str, Any] = {
        "claims": claims,
        "authorized_swarm_lanes": (SWARM_LANE,),
        "slot_decisions": (_decision(),),
        "override_claims": (_override_claim(),),
        "config": CONFIG,
    }
    base.update(over)
    return audit_admissions(ModelWipAuditRequest(**base))


# AC1: the breach counts


def test_a_claim_after_the_limit_reads_one() -> None:
    result = _audit_result(_claim())
    assert result.breaches == 1
    assert result.breach_lanes == ("fixture-lane",)
    assert result.breach_reasons == (EnumWipBreachReason.AT_LIMIT,)
    assert result.line == "AUDIT wip breaches=1"


def test_zero_cases() -> None:
    assert _audit() == 0
    assert _audit(_claim(witness=_witness(reading=_reading(open=4)))) == 0
    assert _audit(_claim(opens_pr=False)) == 0
    assert _audit(_claim(witness=_witness(enforce_ruling=""))) == 0


def test_a_claim_with_no_witness_is_a_breach() -> None:
    result = _audit_result(_claim(witness=None))
    assert result.breaches == 1
    assert result.breach_reasons == (EnumWipBreachReason.NO_WITNESS,)


@pytest.mark.parametrize(
    "witness_over",
    [
        pytest.param({"dispatch_id": OTHER_DISPATCH}, id="other-dispatch"),
        pytest.param({"repo": REPO_B}, id="other-repo"),
    ],
)
def test_a_witness_written_for_another_dispatch_or_repo_is_a_breach(
    witness_over: dict[str, Any],
) -> None:
    result = _audit_result(
        _claim(witness=_witness(reading=_reading(open=1), **witness_over))
    )
    assert result.breaches == 1
    assert result.breach_reasons == (EnumWipBreachReason.WITNESS_MISMATCH,)


# AC1: it never judges against the current policy or the current open PRs


def test_the_request_carries_no_current_policy_and_no_current_state() -> None:
    fields = set(ModelWipAuditRequest.model_fields)
    assert "policy" not in fields
    assert "state" not in fields
    assert "readings" not in fields
    with pytest.raises(ValidationError):
        ModelWipAuditRequest(
            claims=(_claim(),),
            policy=_policy(limit=50),
            slot_decisions=(),
            override_claims=(),
            config=CONFIG,
        )


def test_a_later_raised_limit_leaves_the_breach_counted() -> None:
    # The dispatch saw limit 5 with 9 open. A policy raised to 50 afterwards is not an
    # input of the audit, and the witness's own limit still convicts the claim.
    assert policy_digest(_policy(limit=50)) != policy_digest(_policy())
    assert _audit(_claim(witness=_witness(limit=5))) == 1


def test_a_since_merged_pr_does_not_back_an_exemption_the_witness_cannot_show() -> None:
    # The exemption names PR 42. The reading the dispatch was admitted on did not list
    # it open, so it cannot excuse the claim, whatever the watcher says today.
    exemption = _exemption(kind=EnumWipExemptionKind.OPEN_PR, pr_number=42)
    assert _audit(_claim(witness=_witness(exemption=exemption))) == 1


def test_an_open_pr_exemption_valid_in_the_witnessed_reading_reads_zero_after_it_merged() -> (
    None
):
    exemption = _exemption(kind=EnumWipExemptionKind.OPEN_PR, pr_number=2)
    result = _audit_result(_claim(witness=_witness(exemption=exemption)))
    assert (result.breaches, result.excluded_exempt) == (0, 1)


def test_the_exempt_roles_are_the_witnessed_ones() -> None:
    exemption = _exemption(role="fixture-role-not-exempt-at-dispatch")
    assert _audit(_claim(witness=_witness(exemption=exemption))) == 1


# AC1: exemptions


def test_a_valid_swarm_exemption_reads_zero() -> None:
    result = _audit_result(_claim(witness=_witness(exemption=_exemption())))
    assert (result.breaches, result.excluded_exempt) == (0, 1)


@pytest.mark.parametrize(
    "exemption",
    [
        pytest.param(_exemption(lane_id="fixture-not-named-by-the-stop"), id="forged"),
        pytest.param(
            _exemption(written_by=EnumWipEvidenceWriter.SLOT_REDUCER), id="wrong-writer"
        ),
        pytest.param(_exemption(repo=REPO_B), id="unrelated-repo"),
        pytest.param(
            _exemption(expires_at=NOW - timedelta(minutes=1)), id="expired-at-dispatch"
        ),
    ],
)
def test_an_invalid_exemption_reads_one(exemption: ModelWipExemption) -> None:
    assert _audit(_claim(witness=_witness(exemption=exemption))) == 1


# AC1: overrides


def test_a_valid_override_reads_zero() -> None:
    result = _audit_result(_override_claim_row())
    assert (result.breaches, result.excluded_override) == (0, 1)


def test_a_reserved_floor_override_reads_zero() -> None:
    claim = _claim(
        witness=_witness(
            override=_evidence(reason=EnumWipOverrideReason.RESERVED_FLOOR)
        )
    )
    result = _audit_result(
        claim,
        slot_decisions=(_decision(reason=EnumWipOverrideReason.RESERVED_FLOOR),),
    )
    assert (result.breaches, result.excluded_override) == (0, 1)


def test_a_wip_idle_override_flag_is_not_a_field_of_the_claim() -> None:
    assert "wip_idle_override" not in ModelWipClaimRow.model_fields
    with pytest.raises(ValidationError):
        _claim(wip_idle_override=True)


def _breach_cases() -> list[Any]:
    return [
        pytest.param(
            _claim(
                witness=_witness(
                    override=_evidence(written_by=EnumWipEvidenceWriter.DISPATCHER)
                )
            ),
            {},
            id="forged-writer",
        ),
        pytest.param(
            _claim(
                witness=_witness(override=_evidence(evidence_key="override:slot-1:5"))
            ),
            {},
            id="forged-key",
        ),
        pytest.param(
            _override_claim_row(),
            {"slot_decisions": ()},
            id="forged-no-slot-reducer-decision",
        ),
        pytest.param(
            _override_claim_row(),
            {"slot_decisions": (_decision(decision_id="decision-other"),)},
            id="forged-decision-id-unknown",
        ),
        pytest.param(
            _override_claim_row(),
            {"slot_decisions": (_decision(slot_generation=3),)},
            id="decision-for-another-slot-generation",
        ),
        pytest.param(
            _override_claim_row(),
            {
                "slot_decisions": (
                    _decision(reason=EnumWipOverrideReason.RESERVED_FLOOR),
                )
            },
            id="decision-reason-differs",
        ),
        pytest.param(
            _claim(witness=_witness(override=_evidence(repo=REPO_B))),
            {
                "slot_decisions": (_decision(repo=REPO_B),),
                "override_claims": (_override_claim(repo=REPO_B),),
            },
            id="unrelated-repo",
        ),
        pytest.param(
            _claim(witness=_witness(override=_evidence(dispatch_id=OTHER_DISPATCH))),
            {
                "slot_decisions": (_decision(dispatch_id=OTHER_DISPATCH),),
                "override_claims": (
                    _override_claim(
                        dispatch_id=OTHER_DISPATCH, consumer=OTHER_DISPATCH
                    ),
                ),
            },
            id="unrelated-dispatch",
        ),
        pytest.param(
            _claim(
                witness=_witness(
                    override=_evidence(written_at=NOW - timedelta(minutes=11))
                )
            ),
            {},
            id="expired",
        ),
        pytest.param(
            _override_claim_row(),
            {
                "override_claims": (
                    _override_claim(expires_at=NOW - timedelta(minutes=1)),
                )
            },
            id="expired-claim",
        ),
        pytest.param(
            _override_claim_row(),
            {"override_claims": (_override_claim(consumer=OTHER_DISPATCH),)},
            id="reused-claim-held-by-another-dispatch",
        ),
        pytest.param(
            _override_claim_row(),
            {"override_claims": ()},
            id="no-one-use-claim-on-record",
        ),
        pytest.param(
            _override_claim_row(),
            {"override_claims": (_override_claim(slot_generation=3),)},
            id="claim-for-another-generation",
        ),
    ]


@pytest.mark.parametrize(("claim", "over"), _breach_cases())
def test_an_override_that_does_not_match_the_slot_reducer_and_its_claim_is_a_breach(
    claim: ModelWipClaimRow, over: dict[str, Any]
) -> None:
    result = _audit_result(claim, **over)
    assert result.breaches == 1
    assert result.excluded_override == 0
    assert result.breach_reasons == (EnumWipBreachReason.OVERRIDE_INVALID,)


def test_a_reused_override_counts_only_the_dispatch_that_does_not_hold_the_claim() -> (
    None
):
    first = _override_claim_row()
    second = _claim(
        lane="fixture-lane-2",
        dispatch_id=OTHER_DISPATCH,
        witness=_witness(
            dispatch_id=OTHER_DISPATCH,
            override=_evidence(dispatch_id=OTHER_DISPATCH),
        ),
    )
    result = _audit_result(
        first,
        second,
        slot_decisions=(_decision(), _decision(dispatch_id=OTHER_DISPATCH)),
    )
    assert result.breach_lanes == ("fixture-lane-2",)
    assert result.excluded_override == 1


# AC1: the reading at dispatch


def test_an_unreadable_prior_reading_reads_as_a_breach() -> None:
    reading = ModelWipReading(
        repo=REPO_A,
        readable=False,
        unusable_reason=EnumWipReason.UNREADABLE_STATE,
    )
    result = _audit_result(_claim(witness=_witness(reading=reading)))
    assert result.breaches == 1
    assert result.breach_reasons == (EnumWipBreachReason.UNREADABLE_READING,)


def test_a_reading_with_no_source_time_is_unreadable() -> None:
    reading = ModelWipReading(repo=REPO_A, readable=True, read_at=None, open=0)
    assert _audit(_claim(witness=_witness(reading=reading))) == 1


def test_a_stale_reading_at_dispatch_is_a_breach() -> None:
    stale = _reading(read_at=NOW - timedelta(minutes=11), open=1)
    result = _audit_result(_claim(witness=_witness(reading=stale)))
    assert result.breach_reasons == (EnumWipBreachReason.STALE_READING,)
    wide = CONFIG.model_copy(update={"reading_max_age_minutes": 15})
    assert _audit(_claim(witness=_witness(reading=stale)), config=wide) == 0


def test_a_witness_with_no_limit_is_a_breach_while_enforcing() -> None:
    result = _audit_result(_claim(witness=_witness(limit=None)))
    assert result.breach_reasons == (EnumWipBreachReason.LIMIT_MISSING,)


def test_a_valid_override_does_not_clear_an_unreadable_reading() -> None:
    reading = ModelWipReading(
        repo=REPO_A, readable=False, unusable_reason=EnumWipReason.MISSING_READING
    )
    assert _audit(_override_claim_row(reading=reading)) == 1


def test_a_valid_exemption_reads_zero_with_an_unreadable_reading_as_the_gate_does() -> (
    None
):
    reading = ModelWipReading(
        repo=REPO_A, readable=False, unusable_reason=EnumWipReason.MISSING_READING
    )
    assert (
        _audit(_claim(witness=_witness(reading=reading, exemption=_exemption()))) == 0
    )


def test_the_audit_is_pure_and_repeatable() -> None:
    claims = (_claim(), _override_claim_row(), _claim(witness=None))
    assert _audit_result(*claims) == _audit_result(*claims)


# The gate and the audit agree: the audit convicts exactly what the gate refused.


def _gate_request(**over: Any) -> ModelWipGateRequest:
    prs = {
        f"{REPO_A}#{i}": {
            "queued": False,
            "facts": {
                "repo": REPO_A,
                "number": i,
                "state": "OPEN",
                "draft": False,
                "merged_at": "",
            },
        }
        for i in range(1, 10)
    }
    base: dict[str, Any] = {
        "repo": REPO_A,
        "dispatch_id": DISPATCH,
        "now": NOW,
        "state": {"prs": prs, "last_tick": (NOW - timedelta(minutes=1)).isoformat()},
        "policy": _policy(),
        "config": CONFIG,
        "authorized_swarm_lanes": (SWARM_LANE,),
    }
    base.update(over)
    return ModelWipGateRequest(**base)


@pytest.mark.parametrize(
    ("over", "audit_over"),
    [
        pytest.param({}, {}, id="at-limit"),
        pytest.param({"exemption": _exemption()}, {}, id="swarm-exemption"),
        pytest.param(
            {"exemption": _exemption(kind=EnumWipExemptionKind.OPEN_PR, pr_number=3)},
            {},
            id="open-pr-exemption",
        ),
        pytest.param({"exemption": _exemption(lane_id="x")}, {}, id="forged-exemption"),
        pytest.param({"override": _evidence()}, {}, id="valid-override"),
        pytest.param(
            {"override": _evidence(written_by=EnumWipEvidenceWriter.DISPATCHER)},
            {},
            id="forged-override",
        ),
        pytest.param({"state": None}, {}, id="missing-reading"),
        pytest.param(
            {"state": {"prs": [], "last_tick": "x"}}, {}, id="unreadable-state"
        ),
        pytest.param({"policy": _policy(limit=50)}, {}, id="under-limit"),
        pytest.param({"policy": _policy(enforce="")}, {}, id="not-enforcing"),
    ],
)
def test_the_audit_convicts_exactly_what_the_gate_would_refuse(
    over: dict[str, Any], audit_over: dict[str, Any]
) -> None:
    request = _gate_request(**over)
    decision = evaluate_wip_gate(request, FSM)
    witness = build_admission_witness(request)
    claim = _claim(witness=witness)
    breaches = _audit(claim, **audit_over)
    if decision.outcome is EnumWipOutcome.REFUSE:
        assert breaches == 1, decision.line
    else:
        assert breaches == 0, decision.line


# AC3: the witness is built by the pure rule and rides on the CLAIM row


def test_the_witness_records_what_the_dispatch_was_admitted_on() -> None:
    request = _gate_request(override=_evidence())
    witness = build_admission_witness(request)
    assert witness.repo == REPO_A
    assert witness.dispatch_id == DISPATCH
    assert witness.dispatched_at == NOW
    assert witness.limit == 5
    assert witness.reading.open == 9
    assert witness.reading.read_at == NOW - timedelta(minutes=1)
    assert witness.policy_digest == policy_digest(_policy())
    assert witness.enforce_ruling == RULING
    assert witness.exempt_roles == ("fixture-cleanup",)
    assert witness.override == _evidence()


def test_the_policy_digest_is_stable_and_changes_with_any_policy_field() -> None:
    assert policy_digest(_policy()) == policy_digest(_policy())
    assert len(policy_digest(_policy())) == 64
    assert policy_digest(_policy(limit=6)) != policy_digest(_policy())
    assert policy_digest(_policy(enforce="")) != policy_digest(_policy())


def test_the_witness_cell_round_trips_and_is_safe_in_a_ledger_row() -> None:
    witness = build_admission_witness(_gate_request(exemption=_exemption()))
    cell = encode_witness_cell(witness)
    assert "|" not in cell
    assert "\n" not in cell
    assert " " not in cell
    assert decode_witness_cell(cell) == witness


@pytest.mark.parametrize("cell", ["", "not-base64!", "e30", "bm90LWpzb24"])
def test_a_cell_that_is_not_a_witness_decodes_to_none(cell: str) -> None:
    assert decode_witness_cell(cell) is None


def test_the_override_evidence_gains_a_reason_of_idle_or_reserved_floor() -> None:
    assert {r.value for r in EnumWipOverrideReason} == {"idle", "reserved-floor"}
    with pytest.raises(ValidationError):
        _evidence(reason="other")
    base = _evidence().model_dump()
    del base["reason"]
    with pytest.raises(ValidationError):
        ModelWipOverrideEvidence(**base)


# AC2: the contract fields are unchanged


def test_the_config_fields_are_unchanged() -> None:
    assert set(ModelWipGateConfig.model_fields) == {
        "limit_floor",
        "hours_of_landing",
        "trailing_days",
        "reading_max_age_minutes",
        "override_evidence_minutes",
        "state_outcomes",
    }


# AC3: the rule stays pure


def test_the_rule_module_reads_no_clock_environment_or_store() -> None:
    source = inspect.getsource(rules_wip_gate)
    for forbidden in ("datetime.now", "os.environ", "subprocess", "sqlite3", "open("):
        assert forbidden not in source, forbidden
