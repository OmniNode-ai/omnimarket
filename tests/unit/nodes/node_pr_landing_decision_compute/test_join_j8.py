# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J8 (piece 8 to piece 9): a lab-bound check run on the exact head, read by the landing decision (OMN-22919).

Rebuild plan 4.1, J8: "A lab-bound check run posts on the exact head within the wait bound and the landing decision
node reads it: only a check carrying the current attempt's identity counts: a pass lets the head merge, a failing or
missing check holds it; no public repository reaches a lab runner."

The join is judged the way the floor judges it: piece 8's chain (the check poster) and piece 9's chain (the landing
decision) share one correlation id and the floor's own receipt fold reads the pair. The golden case reads
``golden_only``, the error case ``error_only``, the two together ``passed``. The reading the floor gave before piece 9
answered (piece 8 failed, piece 9 silent past the deadline) is pinned by ``test_control_*`` as ``broken``.

Three injected breaks show the assertions can fail for real: a judge that counts a check of any attempt, a judge that
counts a check posted on another head, and a runner-group read that treats every group as closed to public repositories.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    InMemoryEmissionSink,
    JsonlEmissionSink,
)
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers import (
    handler_pr_landing_decision as decision_module,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_lab_check import (
    public_repos_reaching_lab,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_landing_join_j8 import (
    HandlerLandingJoinJ8,
    load_join_j8_policy,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingLandSkipReason,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_join_j8 import (
    EnumJoinJ8Outcome,
    ModelJoinJ8Request,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_lab_check import (
    ModelRunnerGroupSnapshot,
)
from omnimarket.nodes.node_pr_landing_decision_compute.protocols.runner_groups import (
    InMemoryRunnerGroupSource,
    TextRunnerGroupSource,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 13, 0, 0, tzinfo=UTC)
PR = "OmniNode-ai/omnimarket#9001"
HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
OLD_HEAD = "f7402aa63d1b74b082b174badbe07ac91dcddf36"
CHECK = "lab-tests / run"
CURRENT = "dispatch-41:order-7:overlay-a1"
SUPERSEDED = "dispatch-40:order-7:overlay-a1"
POLICY = load_join_j8_policy()
CHAIN_DEADLINE_S = 900


def _run(attempt: str, conclusion: str = "success", head: str = HEAD) -> dict[str, Any]:
    return {
        "name": CHECK,
        "head_sha": head,
        "attempt_id": attempt,
        "conclusion": conclusion,
    }


def _lab_check(*runs: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "check_name": CHECK,
        "current_attempt_id": CURRENT,
        "runs": list(runs),
        **extra,
    }


def _facts(lab_check: dict[str, Any] | None, **pr: Any) -> ModelLandingFacts:
    row: dict[str, Any] = {
        "pr": PR,
        "head_sha": HEAD,
        "state": "open",
        "ci": "green",
        "merge_state": "clean",
        "created_at": "2026-11-07T11:00:00Z",
        **pr,
    }
    if lab_check is not None:
        row["lab_check"] = lab_check
    return ModelLandingFacts.model_validate(
        {
            "tick": 5,
            "observed_at": NOW.isoformat(),
            "prs": [row],
            "state": {"last_tick": 4, "records": []},
        }
    )


def _merges(facts: ModelLandingFacts) -> list[str]:
    return [
        a.head_sha or ""
        for a in decide_landing(facts).actions
        if a.kind is EnumLandingActionKind.MERGE and a.subject == PR
    ]


def _skip(facts: ModelLandingFacts) -> EnumLandingLandSkipReason | None:
    rows = [s for s in decide_landing(facts).land_skips if s.pr == PR]
    return rows[0].reason if rows else None


class _Clock:
    def __init__(self) -> None:
        self._t = NOW

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


class _Rig:
    """The in-memory backend: an emission sink, a clock, the join handler."""

    def __init__(self) -> None:
        self.sink = InMemoryEmissionSink()
        self.clock = _Clock()
        self.join = HandlerLandingJoinJ8(
            sink=self.sink, policy=POLICY, clock=self.clock, backend="in_memory"
        )

    def upstream_chain(self, cid: str, *, posted: bool) -> None:
        """Piece 8 as the check poster answers: a start, then a success or a typed failure."""
        chain = ChainEmitter(
            self.sink,
            node="node_lab_check_poster",
            piece=POLICY.from_piece,
            clock=self.clock,
            correlation_id=cid,
            backend="in_memory",
        )
        chain.start()
        if posted:
            chain.success({"check_runs_posted": 1})
        else:
            chain.failure("lab_check_not_posted", "no check run reached the head")


def _fold(emissions: list[ModelNodeEmission], now: datetime) -> tuple[str, list[str]]:
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=now,
            emissions=tuple(emissions),
            pieces=tuple(
                ModelPieceDef(piece=p, name=f"piece {p}", reading_max_age_s=86400)
                for p in (POLICY.from_piece, POLICY.to_piece)
            ),
            joins=(
                ModelJoinDef(
                    id=POLICY.join_id,
                    from_piece=POLICY.from_piece,
                    to_piece=POLICY.to_piece,
                    label="ci to landing",
                ),
            ),
            freshness_max_age_s=86400,
            error_max_age_s=86400,
            chain_deadline_s=CHAIN_DEADLINE_S,
        )
    )
    join = next(j for j in receipts.joins if j.join == POLICY.join_id)
    return join.state, [f.code for f in receipts.findings]


# ---------------------------------------------------------------------------------------------- golden


def test_golden_current_attempt_pass_on_the_exact_head_is_mergeable() -> None:
    facts = _facts(_lab_check(_run(CURRENT)))
    assert _merges(facts) == [HEAD]
    assert _skip(facts) is None


def test_golden_a_head_with_no_lab_check_fact_decides_as_before() -> None:
    """The fact is optional: a snapshot without it merges a green, clean head exactly as the node always did."""
    assert _merges(_facts(None)) == [HEAD]


def test_golden_join_chain_reads_golden_on_the_floor() -> None:
    rig = _Rig()
    cid = "j8-golden-1"
    rig.upstream_chain(cid, posted=True)
    result = rig.join.handle(
        ModelJoinJ8Request(
            correlation_id=cid, pr=PR, facts=_facts(_lab_check(_run(CURRENT)))
        )
    )
    assert result.outcome is EnumJoinJ8Outcome.MERGEABLE
    assert result.merge_head == HEAD
    state, findings = _fold(rig.sink.events, NOW + timedelta(minutes=5))
    assert state == "golden_only", findings
    assert findings == []


# ---------------------------------------------------------------------------------------------- error


def test_error_a_missing_check_holds_the_head_and_the_reason_names_missing() -> None:
    facts = _facts(_lab_check())
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.LAB_CHECK_MISSING


def test_error_a_check_from_a_superseded_attempt_holds_the_head_and_names_superseded() -> (
    None
):
    facts = _facts(_lab_check(_run(SUPERSEDED)))
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.LAB_CHECK_SUPERSEDED


def test_error_a_pass_of_the_current_attempt_on_another_head_does_not_count() -> None:
    facts = _facts(_lab_check(_run(CURRENT, head=OLD_HEAD)))
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.LAB_CHECK_MISSING


def test_error_a_superseded_pass_beside_a_current_pass_still_merges_on_the_current_one() -> (
    None
):
    facts = _facts(_lab_check(_run(SUPERSEDED), _run(CURRENT)))
    assert _merges(facts) == [HEAD]


def test_error_a_failing_current_check_holds_and_names_failed() -> None:
    facts = _facts(_lab_check(_run(CURRENT, "failure"), _run(SUPERSEDED)))
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.LAB_CHECK_FAILED


def test_error_a_running_current_check_holds_and_names_pending() -> None:
    facts = _facts(_lab_check(_run(CURRENT, "pending")))
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.LAB_CHECK_PENDING


def test_error_a_check_not_yet_posted_inside_the_wait_bound_is_pending_not_missing() -> (
    None
):
    inside = NOW - timedelta(seconds=60)
    facts = _facts(_lab_check(dispatched_at=inside.isoformat()))
    assert _skip(facts) is EnumLandingLandSkipReason.LAB_CHECK_PENDING
    past = NOW - timedelta(seconds=facts.policy.lab_check_wait_seconds + 1)
    late = _facts(_lab_check(dispatched_at=past.isoformat()))
    assert _skip(late) is EnumLandingLandSkipReason.LAB_CHECK_MISSING


@pytest.mark.parametrize(
    ("runs", "outcome", "code"),
    [
        ((), EnumJoinJ8Outcome.HELD_MISSING, POLICY.missing_failure_code),
        (
            (_run(SUPERSEDED),),
            EnumJoinJ8Outcome.HELD_SUPERSEDED,
            POLICY.superseded_failure_code,
        ),
        (
            (_run(CURRENT, "failure"),),
            EnumJoinJ8Outcome.HELD_FAILED,
            POLICY.failed_failure_code,
        ),
    ],
)
def test_error_join_chain_refuses_typed_and_reads_error_on_the_floor(
    runs: tuple[dict[str, Any], ...], outcome: EnumJoinJ8Outcome, code: str
) -> None:
    rig = _Rig()
    cid = f"j8-error-{outcome.value}"
    rig.upstream_chain(cid, posted=bool(runs))
    result = rig.join.handle(
        ModelJoinJ8Request(correlation_id=cid, pr=PR, facts=_facts(_lab_check(*runs)))
    )
    assert result.outcome is outcome
    assert result.failure_code == code
    assert result.merge_head is None
    piece9 = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in piece9] == ["failure"], "a refusal never started"
    assert piece9[0].failure_code == code
    if not runs:
        state, findings = _fold(rig.sink.events, NOW + timedelta(minutes=5))
        assert state == "error_only", findings
        assert findings == []


def test_error_the_golden_and_the_error_chain_together_read_passed() -> None:
    rig = _Rig()
    rig.upstream_chain("j8-pair-golden", posted=True)
    rig.join.handle(
        ModelJoinJ8Request(
            correlation_id="j8-pair-golden",
            pr=PR,
            facts=_facts(_lab_check(_run(CURRENT))),
        )
    )
    rig.upstream_chain("j8-pair-error", posted=False)
    rig.join.handle(
        ModelJoinJ8Request(
            correlation_id="j8-pair-error", pr=PR, facts=_facts(_lab_check())
        )
    )
    state, findings = _fold(rig.sink.events, NOW + timedelta(minutes=5))
    assert state == "passed", findings


def test_error_a_pending_check_judges_nothing_and_emits_nothing() -> None:
    rig = _Rig()
    result = rig.join.handle(
        ModelJoinJ8Request(
            correlation_id="j8-pending",
            pr=PR,
            facts=_facts(_lab_check(_run(CURRENT, "pending"))),
        )
    )
    assert result.outcome is EnumJoinJ8Outcome.HELD_PENDING
    assert rig.sink.events == []


def test_error_a_pr_with_no_lab_check_fact_is_refused_by_the_join_not_waved_through() -> (
    None
):
    rig = _Rig()
    with pytest.raises(ValueError, match="carries no lab_check"):
        rig.join.handle(
            ModelJoinJ8Request(correlation_id="j8-none", pr=PR, facts=_facts(None))
        )
    assert rig.sink.events == []


def test_error_a_journal_sink_keeps_the_pair_for_the_floor(tmp_path: Path) -> None:
    sink = JsonlEmissionSink(tmp_path / "journal.jsonl")
    clock = _Clock()
    up = ChainEmitter(
        sink,
        node="node_lab_check_poster",
        piece=8,
        clock=clock,
        correlation_id="j8-journal",
        backend="in_memory",
    )
    up.start()
    up.failure("lab_check_not_posted")
    HandlerLandingJoinJ8(
        sink=sink, policy=POLICY, clock=clock, backend="in_memory"
    ).handle(
        ModelJoinJ8Request(
            correlation_id="j8-journal", pr=PR, facts=_facts(_lab_check())
        )
    )
    state, findings = _fold(
        list(sink.read(NOW - timedelta(hours=1))), NOW + timedelta(minutes=5)
    )
    assert state == "error_only", findings


# --------------------------------------------------------------------------------------------- controls


def test_control_the_reading_before_piece_9_answered_is_broken() -> None:
    """Piece 8 failed and piece 9 said nothing past the deadline: the floor's reading before this join."""
    rig = _Rig()
    rig.upstream_chain("j8-control", posted=False)
    state, findings = _fold(
        rig.sink.events, NOW + timedelta(seconds=CHAIN_DEADLINE_S + 60)
    )
    assert state == "broken"
    assert "downstream_missing_typed_failure" in findings


def test_control_break_a_judge_that_counts_any_attempt_merges_a_superseded_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _facts(_lab_check(_run(SUPERSEDED)))
    assert _merges(facts) == []  # the real judge holds it

    real = decision_module.read_lab_check

    def counts_any_attempt(p: Any, now: datetime, policy: Any) -> Any:
        assert p.lab_check is not None
        loose = p.lab_check.model_copy(
            update={
                "runs": tuple(
                    r.model_copy(update={"attempt_id": p.lab_check.current_attempt_id})
                    for r in p.lab_check.runs
                )
            }
        )
        return real(p.model_copy(update={"lab_check": loose}), now, policy)

    monkeypatch.setattr(decision_module, "read_lab_check", counts_any_attempt)
    assert _merges(facts) == [HEAD], "the break must turn the error assertion red"


def test_control_break_a_judge_that_counts_another_head_merges_a_moved_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _facts(_lab_check(_run(CURRENT, head=OLD_HEAD)))
    assert _merges(facts) == []

    real = decision_module.read_lab_check

    def counts_any_head(p: Any, now: datetime, policy: Any) -> Any:
        assert p.lab_check is not None
        loose = p.lab_check.model_copy(
            update={
                "runs": tuple(
                    r.model_copy(update={"head_sha": p.head_sha})
                    for r in p.lab_check.runs
                )
            }
        )
        return real(p.model_copy(update={"lab_check": loose}), now, policy)

    monkeypatch.setattr(decision_module, "read_lab_check", counts_any_head)
    assert _merges(facts) == [HEAD]


# -------------------------------------------------------------------------------------- runner groups


def _snapshot(*groups: dict[str, Any]) -> ModelRunnerGroupSnapshot:
    return ModelRunnerGroupSnapshot.model_validate(
        {
            "observed_at": NOW.isoformat(),
            "repositories": [
                {"name": "acme/private-app", "public": False},
                {"name": "acme/site", "public": True},
                {"name": "acme/lib", "public": True},
            ],
            "groups": list(groups),
        }
    )


LAB_CLOSED = {
    "name": "lab-pool",
    "lab": True,
    "visibility": "selected",
    "allows_public_repositories": False,
    "selected_repositories": ["acme/private-app"],
}
HOSTED_OPEN = {
    "name": "hosted",
    "lab": False,
    "visibility": "all",
    "allows_public_repositories": True,
    "selected_repositories": [],
}


def test_runner_group_golden_no_public_repository_reaches_a_lab_runner() -> None:
    source = InMemoryRunnerGroupSource(_snapshot(LAB_CLOSED, HOSTED_OPEN))
    assert public_repos_reaching_lab(source.read()) == ()


def test_runner_group_error_a_lab_group_open_to_public_repositories_names_them() -> (
    None
):
    open_lab = {**LAB_CLOSED, "allows_public_repositories": True, "visibility": "all"}
    breaches = public_repos_reaching_lab(
        InMemoryRunnerGroupSource(_snapshot(open_lab, HOSTED_OPEN)).read()
    )
    assert {(b.group, b.repository) for b in breaches} == {
        ("lab-pool", "acme/site"),
        ("lab-pool", "acme/lib"),
    }


def test_runner_group_error_a_selected_public_repository_in_an_open_lab_group_is_named() -> (
    None
):
    selected = {
        **LAB_CLOSED,
        "allows_public_repositories": True,
        "selected_repositories": ["acme/site"],
    }
    breaches = public_repos_reaching_lab(_snapshot(selected))
    assert [(b.group, b.repository) for b in breaches] == [("lab-pool", "acme/site")]


def test_runner_group_golden_private_only_visibility_is_closed_to_public_even_if_allowed() -> (
    None
):
    private_only = {
        **LAB_CLOSED,
        "visibility": "private",
        "allows_public_repositories": True,
    }
    assert public_repos_reaching_lab(_snapshot(private_only)) == ()


def test_runner_group_golden_the_text_source_reads_the_saved_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runner_groups.json"
    path.write_text(json.dumps(_snapshot(LAB_CLOSED).model_dump(mode="json")))
    source = TextRunnerGroupSource(path.read_text(), path.name)
    assert public_repos_reaching_lab(source.read()) == ()


@pytest.mark.parametrize("text", ["", "{not json", '{"groups": []}'])
def test_runner_group_error_an_unreadable_snapshot_is_refused_never_read_as_clean(
    text: str,
) -> None:
    with pytest.raises(ValueError, match=r"runner group snapshot saved\.json"):
        TextRunnerGroupSource(text, "saved.json").read()


def test_runner_group_control_break_a_read_that_ignores_the_public_flag_reads_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_lab = {**LAB_CLOSED, "allows_public_repositories": True, "visibility": "all"}
    snapshot = _snapshot(open_lab)
    assert public_repos_reaching_lab(snapshot) != ()  # the real read names a breach
    closed = snapshot.model_copy(
        update={
            "groups": tuple(
                g.model_copy(update={"allows_public_repositories": False})
                for g in snapshot.groups
            )
        }
    )
    assert public_repos_reaching_lab(closed) == (), "the break must turn the read clean"
