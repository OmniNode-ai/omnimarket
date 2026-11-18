# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one eligibility rule: close only when every criterion has a passing check, or (checks absent) a SEEN RUNNING
reading and every criterion ticked. Each test loosens one condition and expects the ticket to be left."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_sprint_closer_effect.handlers.handler_sprint_closer import (
    HandlerSprintCloser,
)
from omnimarket.nodes.node_sprint_closer_effect.handlers.handler_sprint_closer_eligibility import (
    decide_eligibility,
)
from omnimarket.nodes.node_sprint_closer_effect.handlers.sprint_closer_sources import (
    last_fingerprint,
    map_dod_checks,
    parse_criteria,
    read_seen_running,
)
from omnimarket.nodes.node_sprint_closer_effect.models import (
    EnumCheckStatus,
    EnumCloserAction,
    EnumCloserVerdict,
    ModelCheckResult,
    ModelTicketEvidence,
)

pytestmark = pytest.mark.unit

PASS, FAIL, NOT_RUN = (
    EnumCheckStatus.PASS,
    EnumCheckStatus.FAIL,
    EnumCheckStatus.NOT_RUN,
)


def _ev(**kw: object) -> ModelTicketEvidence:
    base: dict[str, object] = {
        "ticket_id": "OMN-1",
        "criteria": ("AC1", "AC2"),
        "contract_ref": "OmniNode-ai/omnimarket@abc:contracts/OMN-1.yaml",
        "change_kind": "non_runtime",
        "linked_prs": ("omninode-ai/example#1",),
        "checks": (
            ModelCheckResult(evidence_id="c1", status=PASS, binds=("AC1",)),
            ModelCheckResult(evidence_id="c2", status=PASS, binds=("AC2",)),
        ),
    }
    base.update(kw)
    return ModelTicketEvidence.model_validate(base)


def test_all_checks_passing_closes() -> None:
    el = decide_eligibility(_ev())
    assert el.verdict is EnumCloserVerdict.CLOSE
    assert el.basis == "checks"
    assert not el.missing
    assert any("c1 passed (binds AC1)" in e for e in el.evidence)


def test_unbound_criterion_is_left() -> None:
    el = decide_eligibility(_ev(criteria=("AC1", "AC2", "AC3")))
    assert el.verdict is EnumCloserVerdict.LEAVE
    assert any("AC3 is bound by no dod_verify check" in m for m in el.missing)


@pytest.mark.parametrize("status", [FAIL, NOT_RUN])
def test_failing_or_unrun_check_is_left(status: EnumCheckStatus) -> None:
    checks = (
        ModelCheckResult(evidence_id="c1", status=PASS, binds=("AC1",)),
        ModelCheckResult(
            evidence_id="c2", status=status, binds=("AC2",), detail="exit 1"
        ),
    )
    el = decide_eligibility(_ev(checks=checks))
    assert el.verdict is EnumCloserVerdict.LEAVE
    assert any("c2" in m and "exit 1" in m for m in el.missing)


def test_failing_unbound_gate_check_is_left() -> None:
    checks = (
        ModelCheckResult(evidence_id="c1", status=PASS, binds=("AC1",)),
        ModelCheckResult(evidence_id="c2", status=PASS, binds=("AC2",)),
        ModelCheckResult(evidence_id="durable_gate::contract_on_occ_main", status=FAIL),
    )
    assert decide_eligibility(_ev(checks=checks)).verdict is EnumCloserVerdict.LEAVE


def test_checks_absent_without_seen_running_is_left() -> None:
    el = decide_eligibility(
        _ev(
            checks=(),
            contract_error="CONTRACT_MISSING: none",
            change_kind="runtime",
            criteria_ticked=("AC1", "AC2"),
        )
    )
    assert el.verdict is EnumCloserVerdict.LEAVE
    assert any("no SEEN RUNNING reading" in m for m in el.missing)


def test_checks_absent_seen_running_but_criterion_unticked_is_left() -> None:
    el = decide_eligibility(
        _ev(
            checks=(),
            contract_error="CONTRACT_MISSING: none",
            change_kind="runtime",
            seen_running=("ledger row t",),
            criteria_ticked=("AC1",),
        )
    )
    assert el.verdict is EnumCloserVerdict.LEAVE
    assert any("AC2" in m for m in el.missing)


def test_checks_absent_seen_running_and_criteria_met_closes() -> None:
    el = decide_eligibility(
        _ev(
            checks=(),
            contract_error="CONTRACT_MISSING: none",
            change_kind="runtime",
            seen_running=("ledger row t",),
            criteria_ticked=("AC1", "AC2"),
        )
    )
    assert el.verdict is EnumCloserVerdict.CLOSE
    assert el.basis == "seen_running"


def test_no_criteria_is_left() -> None:
    assert decide_eligibility(_ev(criteria=())).verdict is EnumCloserVerdict.LEAVE


def test_collaborator_ticket_gets_evidence_comment_not_done() -> None:
    d = HandlerSprintCloser("t (m)").handle(
        _ev(is_collaborator=True, assignee="Someone Else")
    )
    assert d.action is EnumCloserAction.COMMENT_EVIDENCE
    assert d.comment is not None
    assert d.comment.startswith("actor: t (m)\n")


def test_owner_ticket_is_set_done_and_unchanged_reason_is_not_recommented() -> None:
    assert (
        HandlerSprintCloser("t (m)").handle(_ev()).action is EnumCloserAction.SET_DONE
    )
    left = _ev(criteria=("AC1", "AC2", "AC3"))
    first = HandlerSprintCloser("t (m)").handle(left)
    assert first.action is EnumCloserAction.COMMENT_MISSING
    assert first.comment
    assert last_fingerprint([first.comment]) == first.fingerprint
    again = HandlerSprintCloser("other (m)", first.fingerprint).handle(left)
    assert again.action is EnumCloserAction.NO_CHANGE
    assert again.comment is None


def test_parse_criteria_and_dod_mapping() -> None:
    labels, ticked = parse_criteria(
        "## AC\n\n- [ ] AC1: a\n- [x] AC2: b\n- [ ] AC1: dup\ntext AC9\n"
    )
    assert labels == ("AC1", "AC2")
    assert ticked == ("AC2",)
    ref, err, checks = map_dod_checks(
        {
            "contract_repository": "OmniNode-ai/x",
            "contract_commit_sha": "0123456789abcdef",
            "contract_repo_path": "c",
            "checks": [
                {"evidence_id": "a", "status": "verified", "binds_ac": ["AC1"]},
                {
                    "evidence_id": "b",
                    "status": "skipped",
                    "binds_ac": ["AC2"],
                    "message": "stale",
                },
            ],
        }
    )
    assert ref == "OmniNode-ai/x@0123456789ab:c"
    assert err is None
    assert [c.status for c in checks] == [PASS, NOT_RUN]
    ref, err, checks = map_dod_checks(
        {
            "checks": [
                {
                    "evidence_id": "contract",
                    "status": "failed",
                    "message": "CONTRACT_MISSING: none",
                }
            ]
        }
    )
    assert ref is None
    assert checks == ()
    assert err == "CONTRACT_MISSING: none"


def test_passing_runtime_checks_cannot_close_without_seen_running() -> None:
    issue = _ev(change_kind="runtime")
    result = decide_eligibility(issue)
    assert result.verdict is EnumCloserVerdict.LEAVE
    assert any("no SEEN RUNNING reading" in reason for reason in result.missing)
    with_reading = issue.model_copy(update={"seen_running": ("ledger row t",)})
    assert decide_eligibility(with_reading).verdict is EnumCloserVerdict.CLOSE


def test_missing_contract_closes_on_merged_non_runtime_prs() -> None:
    evidence = ModelTicketEvidence.model_validate(
        {
            "ticket_id": "OMN-1",
            "contract_error": "CONTRACT_MISSING: none",
            "change_kind": "non_runtime",
            "linked_prs": ("omninode-ai/example#1", "omninode-ai/example#2"),
        }
    )
    result = decide_eligibility(evidence)
    assert result.verdict is EnumCloserVerdict.CLOSE
    assert result.basis == "linked_prs"
    assert all(
        any(pr in line for line in result.evidence) for pr in evidence.linked_prs
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"linked_prs": ()},
        {"pr_blockers": ("linked PR example#2 is open",)},
        {"pr_blockers": ("linked PR example#2 state is unread",)},
        {"contract_error": "verifier timed out"},
    ],
)
def test_missing_contract_fallback_requires_complete_merged_pr_evidence(
    changes: dict[str, object],
) -> None:
    evidence = _ev(checks=(), contract_error="CONTRACT_MISSING: none")
    assert (
        decide_eligibility(evidence.model_copy(update=changes)).verdict
        is EnumCloserVerdict.LEAVE
    )


def test_ledger_reader_requires_affirmative_running_verdict(tmp_path: Path) -> None:
    ledger = tmp_path / "integration.log"
    ledger.write_text(
        "2026-11-18T10:00:00Z | STATUS | ticket=OMN-1 | observed=NOT SEEN RUNNING\n"
        "2026-11-18T10:10:00Z | STATUS | ticket=OMN-1 | a SEEN RUNNING reading is needed\n"
        "2026-11-18T10:20:00Z | STATUS | ticket=OMN-11 | verdict=SEEN_RUNNING\n"
        "2026-11-18T10:30:00Z | STATUS | ticket=OMN-1 | verdict=SEEN_RUNNING\n"
    )
    assert read_seen_running("OMN-1", ("cat", str(ledger))) == (
        "ledger row 2026-11-18T10:30:00Z",
    )
