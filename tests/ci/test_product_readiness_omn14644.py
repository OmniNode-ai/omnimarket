# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for Product Readiness split + deterministic head freeze (OMN-14644, WS1).

The three WS1 acceptance proofs are non-vacuous:

1. **Red-before-OCC.** A product-readiness failure blocks any head-bound OCC
   evidence from being considered valid — at the CI classifier boundary
   (``freeze_eligible=False``) *and* at the model boundary
   (``FreezeLedger.freeze`` / ``ModelHeadFreezeRecord`` reject a non-green head).
2. **Exactly one freeze per green head, idempotent replay.** Freezing an
   identical tuple twice yields the same record and no second freeze / no
   supersession.
3. **Supersession, not stale reuse.** A synchronize / base-retarget /
   contract- or policy-digest / ticket-set change on the same PR emits a
   supersession record and marks the prior freeze stale, so old evidence can no
   longer satisfy Governance Readiness.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.events.head_freeze import (
    EnumFreezeSupersedeReason,
    EnumProductReadinessOutcome,
    FreezeLedger,
    ModelFrozenTuple,
    ModelHeadFreezeRecord,
    ModelHeadFreezeSupersession,
    classify_supersede_reason,
    product_outcome_is_freeze_eligible,
)
from scripts.ci.product_readiness import (
    CHANGE_DETECTION_FAILED,
    COVERAGE_FAILED,
    LINT_FAILED,
    PRODUCT_GREEN,
    PRODUCT_INFRA,
    TEST_FAILED,
    TYPE_FAILED,
    ProductFacts,
    classify,
)

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "ci" / "product_readiness.py"
)
_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _tuple(**overrides: object) -> ModelFrozenTuple:
    base: dict[str, object] = {
        "repo": "omnimarket",
        "pr_number": 1784,
        "ticket_set": ("OMN-14644",),
        "head_sha": "a" * 40,
        "base_ref": "dev",
        "contract_digest": "sha256:contract-v1",
        "policy_digest": "sha256:policy-v1",
    }
    base.update(overrides)
    return ModelFrozenTuple(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 1. Red-before-OCC — a non-green head cannot mint head-bound evidence.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_only_green_is_freeze_eligible() -> None:
    assert product_outcome_is_freeze_eligible(EnumProductReadinessOutcome.PRODUCT_GREEN)
    for outcome in EnumProductReadinessOutcome:
        if outcome is EnumProductReadinessOutcome.PRODUCT_GREEN:
            continue
        assert not product_outcome_is_freeze_eligible(outcome), outcome


@pytest.mark.unit
def test_freeze_record_rejects_non_green_outcome() -> None:
    with pytest.raises(ValidationError, match="only valid for a PRODUCT_GREEN"):
        ModelHeadFreezeRecord(
            frozen_tuple=_tuple(),
            product_outcome=EnumProductReadinessOutcome.TEST_FAILED,
            superseder="ci-bot",
            created_at=_NOW,
        )


@pytest.mark.unit
def test_ledger_refuses_to_freeze_non_green_head() -> None:
    ledger = FreezeLedger()
    with pytest.raises(ValueError, match="not PRODUCT_GREEN"):
        ledger.freeze(
            _tuple(),
            product_outcome=EnumProductReadinessOutcome.LINT_FAILED,
            superseder="ci-bot",
            created_at=_NOW,
        )
    # Nothing was recorded — no head-bound evidence exists for a red head.
    assert ledger.records() == ()
    assert ledger.active_for("omnimarket", 1784) is None


@pytest.mark.unit
def test_classifier_red_product_test_is_not_freeze_eligible() -> None:
    result = classify(
        ProductFacts.from_dict(
            {
                "change_detection": "success",
                "lint": "success",
                "typecheck": "success",
                "tests": "failure",
                "coverage": "success",
            }
        )
    )
    assert result.outcome == TEST_FAILED
    assert result.freeze_eligible is False


# --------------------------------------------------------------------------
# 2. Exactly one freeze per green head; idempotent replay.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_green_head_creates_exactly_one_freeze() -> None:
    ledger = FreezeLedger()
    rec = ledger.freeze(
        _tuple(),
        product_outcome=EnumProductReadinessOutcome.PRODUCT_GREEN,
        superseder="ci-bot",
        created_at=_NOW,
    )
    assert rec.product_outcome is EnumProductReadinessOutcome.PRODUCT_GREEN
    assert len(ledger.records()) == 1
    assert ledger.active_for("omnimarket", 1784) is rec
    assert ledger.supersessions() == ()


@pytest.mark.unit
def test_replay_of_same_tuple_is_idempotent() -> None:
    ledger = FreezeLedger()
    frozen = _tuple()
    first = ledger.freeze(
        frozen,
        product_outcome=EnumProductReadinessOutcome.PRODUCT_GREEN,
        superseder="ci-bot",
        created_at=_NOW,
    )
    # Replay with a *later* timestamp — must still dedupe to the same record.
    second = ledger.freeze(
        _tuple(),
        product_outcome=EnumProductReadinessOutcome.PRODUCT_GREEN,
        superseder="ci-bot",
        created_at=datetime(2026, 7, 16, 13, 0, 0, tzinfo=UTC),
    )
    assert first == second
    assert first.freeze_id == second.freeze_id
    assert len(ledger.records()) == 1
    assert ledger.supersessions() == ()
    assert not ledger.is_superseded(first.freeze_id)


# --------------------------------------------------------------------------
# 3. Supersession — a moved tuple invalidates the prior freeze.
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("override", "expected_reason"),
    [
        ({"head_sha": "b" * 40}, EnumFreezeSupersedeReason.SYNCHRONIZE),
        ({"base_ref": "main"}, EnumFreezeSupersedeReason.BASE_RETARGET),
        (
            {"contract_digest": "sha256:contract-v2"},
            EnumFreezeSupersedeReason.CONTRACT_DIGEST_CHANGE,
        ),
        (
            {"policy_digest": "sha256:policy-v2"},
            EnumFreezeSupersedeReason.POLICY_DIGEST_CHANGE,
        ),
        (
            {"ticket_set": ("OMN-14644", "OMN-14643")},
            EnumFreezeSupersedeReason.TICKET_SET_CHANGE,
        ),
    ],
)
def test_moved_tuple_emits_supersession_not_stale_reuse(
    override: dict[str, object], expected_reason: EnumFreezeSupersedeReason
) -> None:
    ledger = FreezeLedger()
    old = ledger.freeze(
        _tuple(),
        product_outcome=EnumProductReadinessOutcome.PRODUCT_GREEN,
        superseder="ci-bot",
        created_at=_NOW,
    )
    new = ledger.freeze(
        _tuple(**override),
        product_outcome=EnumProductReadinessOutcome.PRODUCT_GREEN,
        superseder="ci-bot",
        created_at=datetime(2026, 7, 16, 14, 0, 0, tzinfo=UTC),
    )
    # A genuinely new frozen head — distinct key, distinct record.
    assert new.freeze_id != old.freeze_id
    assert len(ledger.records()) == 2
    # Exactly one supersession, carrying the right reason and the stale key.
    (supersession,) = ledger.supersessions()
    assert supersession.reason is expected_reason
    assert supersession.superseded_freeze_id == old.freeze_id
    assert supersession.new_tuple.freeze_id == new.freeze_id
    # Old evidence can no longer satisfy Governance Readiness.
    assert ledger.is_superseded(old.freeze_id)
    assert ledger.active_for("omnimarket", 1784) is new


@pytest.mark.unit
def test_synchronize_outranks_other_axes() -> None:
    # When several axes change at once, head-move (SYNCHRONIZE) wins.
    reason = classify_supersede_reason(
        _tuple(),
        _tuple(head_sha="b" * 40, base_ref="main", contract_digest="x"),
    )
    assert reason is EnumFreezeSupersedeReason.SYNCHRONIZE


@pytest.mark.unit
def test_classify_supersede_reason_identical_is_none() -> None:
    assert classify_supersede_reason(_tuple(), _tuple()) is None


@pytest.mark.unit
def test_classify_supersede_reason_requires_same_pr() -> None:
    with pytest.raises(ValueError, match="same \\(repo, pr_number\\)"):
        classify_supersede_reason(_tuple(), _tuple(pr_number=9999))


# --------------------------------------------------------------------------
# Frozen-tuple identity — deterministic, order-independent fingerprint.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_freeze_id_is_deterministic_and_head_sensitive() -> None:
    assert _tuple().freeze_id == _tuple().freeze_id
    assert _tuple().freeze_id != _tuple(head_sha="b" * 40).freeze_id
    assert len(_tuple().freeze_id) == 16


@pytest.mark.unit
def test_ticket_set_is_order_and_dup_independent() -> None:
    a = _tuple(ticket_set=("OMN-14644", "OMN-14643"))
    b = _tuple(ticket_set=("OMN-14643", "OMN-14644", "OMN-14644"))
    assert a.ticket_set == ("OMN-14643", "OMN-14644")
    assert a.freeze_id == b.freeze_id


@pytest.mark.unit
def test_ticket_set_rejects_non_omn_and_empty() -> None:
    with pytest.raises(ValidationError):
        _tuple(ticket_set=("not-a-ticket",))
    with pytest.raises(ValidationError):
        _tuple(ticket_set=())


# --------------------------------------------------------------------------
# Supersession model validators.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_supersession_rejects_reason_mismatch() -> None:
    old = _tuple()
    new = _tuple(head_sha="b" * 40)  # a SYNCHRONIZE change
    with pytest.raises(ValidationError, match="does not match"):
        ModelHeadFreezeSupersession(
            superseded_freeze_id=old.freeze_id,
            superseded_tuple=old,
            new_tuple=new,
            reason=EnumFreezeSupersedeReason.BASE_RETARGET,  # wrong axis
            superseder="ci-bot",
            created_at=_NOW,
        )


@pytest.mark.unit
def test_supersession_rejects_identical_tuple() -> None:
    frozen = _tuple()
    with pytest.raises(ValidationError, match="differ from the superseded tuple"):
        ModelHeadFreezeSupersession(
            superseded_freeze_id=frozen.freeze_id,
            superseded_tuple=frozen,
            new_tuple=_tuple(),
            reason=EnumFreezeSupersedeReason.SYNCHRONIZE,
            superseder="ci-bot",
            created_at=_NOW,
        )


@pytest.mark.unit
def test_supersession_rejects_mismatched_freeze_id() -> None:
    old = _tuple()
    new = _tuple(head_sha="b" * 40)
    with pytest.raises(
        ValidationError, match=r"must equal superseded_tuple\.freeze_id"
    ):
        ModelHeadFreezeSupersession(
            superseded_freeze_id="deadbeefdeadbeef",
            superseded_tuple=old,
            new_tuple=new,
            reason=EnumFreezeSupersedeReason.SYNCHRONIZE,
            superseder="ci-bot",
            created_at=_NOW,
        )


@pytest.mark.unit
def test_freeze_record_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        ModelHeadFreezeRecord(
            frozen_tuple=_tuple(),
            product_outcome=EnumProductReadinessOutcome.PRODUCT_GREEN,
            superseder="ci-bot",
            created_at=datetime(2026, 7, 16, 12, 0, 0),  # naive
        )


# --------------------------------------------------------------------------
# Classifier — precedence, fail-closed, and CLI parity.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_all_green_is_product_green_and_eligible() -> None:
    result = classify(
        ProductFacts.from_dict(
            {
                "change_detection": "success",
                "lint": "success",
                "typecheck": "success",
                "tests": "success",
                "coverage": "success",
            }
        )
    )
    assert result.outcome == PRODUCT_GREEN
    assert result.freeze_eligible is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("failing", "expected"),
    [
        ("change_detection", CHANGE_DETECTION_FAILED),
        ("lint", LINT_FAILED),
        ("typecheck", TYPE_FAILED),
        ("tests", TEST_FAILED),
        ("coverage", COVERAGE_FAILED),
    ],
)
def test_each_subcheck_failure_maps_to_its_outcome(failing: str, expected: str) -> None:
    facts = dict.fromkeys(
        ("change_detection", "lint", "typecheck", "tests", "coverage"), "success"
    )
    facts[failing] = "failure"
    assert classify(ProductFacts.from_dict(facts)).outcome == expected


@pytest.mark.unit
def test_affirmative_failure_outranks_infra() -> None:
    # change-detection cancelled (infra) but lint affirmatively failed → lint wins.
    result = classify(
        ProductFacts.from_dict(
            {
                "change_detection": "cancelled",
                "lint": "failure",
                "typecheck": "success",
                "tests": "success",
                "coverage": "success",
            }
        )
    )
    assert result.outcome == LINT_FAILED


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad", ["cancelled", "skipped", "", "timed_out", "in_progress"]
)
def test_unconfirmed_subcheck_fails_closed_to_infra(bad: str) -> None:
    facts = dict.fromkeys(
        ("change_detection", "lint", "typecheck", "tests", "coverage"), "success"
    )
    facts["coverage"] = bad
    result = classify(ProductFacts.from_dict(facts))
    assert result.outcome == PRODUCT_INFRA
    assert result.freeze_eligible is False


@pytest.mark.unit
def test_missing_facts_fail_closed() -> None:
    # No subcheck reported at all → every subcheck absent → product_infra.
    result = classify(ProductFacts.from_dict({}))
    assert result.outcome == PRODUCT_INFRA
    assert result.freeze_eligible is False


@pytest.mark.unit
def test_script_outcome_constants_mirror_canonical_enum() -> None:
    # DRY guard: the stdlib-only script's outcome strings must exactly mirror the
    # canonical EnumProductReadinessOutcome values (no drift between the two).
    script_values = {
        PRODUCT_GREEN,
        CHANGE_DETECTION_FAILED,
        LINT_FAILED,
        TYPE_FAILED,
        TEST_FAILED,
        COVERAGE_FAILED,
        PRODUCT_INFRA,
    }
    enum_values = {o.value for o in EnumProductReadinessOutcome}
    assert script_values == enum_values


# --------------------------------------------------------------------------
# CLI surface — report-only vs enforcement exit codes.
# --------------------------------------------------------------------------


def _run_cli(
    facts: dict[str, str], report_only: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "classify",
            "--facts-json",
            json.dumps(facts),
            "--report-only",
            report_only,
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
def test_cli_report_only_never_fails_on_red() -> None:
    proc = _run_cli({"lint": "failure"}, "true")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["outcome"] == LINT_FAILED
    assert payload["freeze_eligible"] is False


@pytest.mark.unit
def test_cli_enforcement_fails_on_red_but_passes_on_green() -> None:
    red = _run_cli({"lint": "failure"}, "false")
    assert red.returncode == 1
    green = _run_cli(
        {
            "change_detection": "success",
            "lint": "success",
            "typecheck": "success",
            "tests": "success",
            "coverage": "success",
        },
        "false",
    )
    assert green.returncode == 0
    assert json.loads(green.stdout)["freeze_eligible"] is True
