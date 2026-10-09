# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20675: the DoD closeout sweep decisions, contract to bus to typed result, and refusals.

The retired workflow asked agents to decide these in prose, so each case below is a rule
the workflow text states: the idempotency guard's three verdicts and its fail direction,
the live sprint resolution that never picks among several, and the four exclusions that
make a ticket a candidate. Golden chain: the registered contract on the in-memory bus.
Error chain: every refusal happens before the handler runs, or in it as a ValueError.
"""

from __future__ import annotations

import importlib
import json
import re
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_dod_closeout_sweep_compute as node_package
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    HandlerDodCloseoutSweep,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    EnumCloseoutAction,
    EnumPrecheckVerdict,
    ModelDodCloseoutDecisionRequest,
    ModelDodCloseoutDecisionResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.dod-closeout-sweep-decide-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.dod-closeout-sweep-decided.v1"
HANDLER = HandlerDodCloseoutSweep()
DATE = "2026-09-24"
CLOCK = "2026-09-24T14:00:00Z"
UUID_A = "a7530301-012d-4bd8-9c67-b24fbe21d93f"
UUID_B = "11111111-2222-3333-4444-555555555555"

CONFORMANT_REPORT = "\n".join(
    [
        "# DoD closeout sweep 2026-09-24",
        "## Counts",
        "| flipped | held | merged-unreleased | corrected | reverted | fenced | parents | external |",
        "|---|---|---|---|---|---|---|---|",
        "| 0 | 14 | 0 | 3 | 0 | 37 | 11 | 15 |",
        "## Flipped",
        "| ticket | PR | merge sha | receipt |",
        "## Held",
        "| ticket | unmet check |",
        "## Blocking-check-class histogram",
        "| ac:unbound | 12 |",
    ]
)
TERMINAL_OK = (
    f"2026-09-24T14:03:45Z | TERMINAL | lane=dod-closeout-sweep | date={DATE} | friction=none"
    " | candidates=14 [cite: r] | flipped=0 [cite: r] | held=14 | merged_unreleased=0"
    " | corrected=3 | reverted=0"
)


def decide(**fields: Any) -> ModelDodCloseoutDecisionResult:
    return HANDLER.handle(ModelDodCloseoutDecisionRequest.model_validate(fields))


def precheck(rows: list[str], **fields: Any) -> ModelDodCloseoutDecisionResult:
    return decide(
        kind="precheck", date=DATE, clock_utc=CLOCK, ledger_rows=rows, **fields
    )


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeDodCloseoutSweepCompute, handler_type)
    for side, model in (
        ("input_model", ModelDodCloseoutDecisionRequest),
        ("output_model", ModelDodCloseoutDecisionResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_dod_closeout_sweep_compute"].load() is node_package


def test_contract_kind_enum_is_the_set_the_handler_decides() -> None:
    declared = set(_contract()["inputs"]["kind"]["enum"])
    from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
        EnumCloseoutDecisionKind,
    )

    assert declared == {k.value for k in EnumCloseoutDecisionKind}


# --- precheck -----------------------------------------------------------------


def test_force_bypasses_the_guard_even_when_delivered() -> None:
    result = precheck(
        [TERMINAL_OK],
        force=True,
        report_text=CONFORMANT_REPORT,
        report_commit_sha="abc123",
    )
    assert result.verdict is EnumPrecheckVerdict.RUN
    assert result.evidence == "force=true"


def test_committed_conformant_report_and_terminal_with_counts_is_already_delivered() -> (
    None
):
    result = precheck(
        [TERMINAL_OK], report_text=CONFORMANT_REPORT, report_commit_sha="abc123"
    )
    assert result.verdict is EnumPrecheckVerdict.ALREADY_DELIVERED
    assert result.evidence == "abc123 2026-09-24T14:03:45Z"
    assert result.checks_failed == []
    assert result.report_lines == len(CONFORMANT_REPORT.split("\n"))


@pytest.mark.parametrize(
    ("fields", "failed"),
    [
        ({"report_text": None, "report_commit_sha": ""}, "report-missing"),
        (
            {"report_text": CONFORMANT_REPORT, "report_commit_sha": ""},
            "report-uncommitted",
        ),
        (
            {
                "report_text": CONFORMANT_REPORT,
                "report_commit_sha": "abc",
                "report_dirty": True,
            },
            "report-dirty",
        ),
        (
            {
                "report_text": CONFORMANT_REPORT + "\nfiller" * 150,
                "report_commit_sha": "abc",
            },
            "report-lines>150",
        ),
        (
            {
                "report_text": CONFORMANT_REPORT.replace("fenced", "walled"),
                "report_commit_sha": "abc",
            },
            "report-counts-block-missing:fenced",
        ),
        (
            {
                "report_text": CONFORMANT_REPORT.replace("receipt", "proof"),
                "report_commit_sha": "abc",
            },
            "report-flipped-table-missing",
        ),
        (
            {
                "report_text": CONFORMANT_REPORT.replace("unmet", "gap"),
                "report_commit_sha": "abc",
            },
            "report-held-table-missing",
        ),
        (
            {
                "report_text": CONFORMANT_REPORT.replace("histogram", "list"),
                "report_commit_sha": "abc",
            },
            "report-histogram-missing",
        ),
    ],
)
def test_one_failed_report_check_is_a_run(fields: dict[str, Any], failed: str) -> None:
    result = precheck([TERMINAL_OK], **fields)
    assert result.verdict is EnumPrecheckVerdict.RUN
    assert result.checks_failed is not None
    assert failed in result.checks_failed
    if failed == "report-lines>150":
        assert result.report_lines == len(fields["report_text"].split("\n"))
        assert result.report_lines > 150


@pytest.mark.parametrize(
    "row",
    [
        # no counts on the TERMINAL
        f"2026-09-24T14:03:45Z | TERMINAL | lane=dod-closeout-sweep | date={DATE} | friction=none | outcome=done",
        # a different date
        TERMINAL_OK.replace(f"date={DATE}", "date=2026-09-23"),
        # a different lane
        TERMINAL_OK.replace("lane=dod-closeout-sweep", "lane=dod-closeout-sweep-2"),
        # lane merged into another cell: not a lane cell
        TERMINAL_OK.replace("lane=dod-closeout-sweep | ", "x=lane=dod-closeout-sweep "),
    ],
)
def test_a_terminal_that_is_not_this_dates_counted_delivery_does_not_count(
    row: str,
) -> None:
    result = precheck([row], report_text=CONFORMANT_REPORT, report_commit_sha="abc")
    assert result.verdict is EnumPrecheckVerdict.RUN
    assert result.checks_failed == ["ledger-terminal-missing"]


def test_a_live_claim_without_terminal_is_peer_owned() -> None:
    claim = "2026-09-24T12:30:00Z | CLAIM | lane=dod-closeout-sweep | ticket=OMN-1"
    result = precheck([claim])
    assert result.verdict is EnumPrecheckVerdict.PEER_OWNED
    assert result.evidence == "2026-09-24T12:30:00Z"


def test_delivered_beats_peer_owned() -> None:
    claim = "2026-09-24T13:59:00Z | CLAIM | lane=dod-closeout-sweep | ticket=OMN-1"
    result = precheck(
        [claim, TERMINAL_OK], report_text=CONFORMANT_REPORT, report_commit_sha="abc"
    )
    assert result.verdict is EnumPrecheckVerdict.ALREADY_DELIVERED


def test_a_claim_180_minutes_old_is_stale_and_runs() -> None:
    claim = "2026-09-24T11:00:00Z | CLAIM | lane=dod-closeout-sweep | ticket=OMN-1"
    result = precheck([claim])
    assert result.verdict is EnumPrecheckVerdict.RUN
    assert result.reason is not None
    assert "stale CLAIM" in result.reason
    assert "2026-09-24T11:00:00Z" in result.reason
    just_inside = (
        "2026-09-24T11:00:01Z | CLAIM | lane=dod-closeout-sweep | ticket=OMN-1"
    )
    assert precheck([just_inside]).verdict is EnumPrecheckVerdict.PEER_OWNED


def test_a_claim_stamped_in_the_future_is_age_zero() -> None:
    claim = "2026-09-24T14:10:00Z | CLAIM | lane=dod-closeout-sweep | ticket=OMN-1"
    assert precheck([claim]).verdict is EnumPrecheckVerdict.PEER_OWNED


def test_a_terminal_at_or_after_a_claim_answers_it() -> None:
    claim = "2026-09-24T13:00:00Z | CLAIM | lane=dod-closeout-sweep | ticket=OMN-1"
    later = "2026-09-24T13:00:00Z | TERMINAL | lane=dod-closeout-sweep | outcome=failed"
    assert precheck([claim, later]).verdict is EnumPrecheckVerdict.RUN
    earlier = (
        "2026-09-24T12:59:59Z | TERMINAL | lane=dod-closeout-sweep | outcome=failed"
    )
    assert precheck([claim, earlier]).verdict is EnumPrecheckVerdict.PEER_OWNED


def test_rows_of_other_lanes_and_unparseable_rows_never_hold_a_claim() -> None:
    rows = [
        "2026-09-24T13:59:00Z | CLAIM | lane=dod-closeout-chunk0 | ticket=OMN-1",
        "2026-09-24T13:59:00Z | CLAIM | lane=dod-closeout-sweep-r | ticket=OMN-1",
        "not a ledger row",
        "2026-13-45T99:99:99Z | CLAIM | lane=dod-closeout-sweep | ticket=OMN-1",
    ]
    assert precheck(rows).verdict is EnumPrecheckVerdict.RUN


# --- resolve_sprint -----------------------------------------------------------


def _project(
    name: str,
    start: str,
    end: str,
    done: str | None = None,
    uuid: str = UUID_A,
    pid: str = "P-OMN-28",
) -> dict[str, Any]:
    return {
        "id": pid,
        "uuid": uuid,
        "name": name,
        "start_date": start,
        "target_date": end,
        "completed_at": done,
    }


def resolve(date: str, projects: list[dict[str, Any]], **fields: Any) -> Any:
    return decide(kind="resolve_sprint", date=date, projects=projects, **fields)


def test_the_one_containing_sprint_resolves_to_its_uuid_not_its_short_id() -> None:
    result = resolve(
        DATE,
        [
            _project(
                "Sprint 2026-09-21 → 2026-09-28 (Beta)", "2026-09-21", "2026-09-28"
            ),
            _project(
                "Sprint 2026-09-14 -> 2026-09-21 (Beta)",
                "2026-09-14",
                "2026-09-21",
                uuid=UUID_B,
                pid="P-OMN-27",
            ),
            _project(
                "Platform roadmap",
                "2026-01-01",
                "2026-12-31",
                uuid=UUID_B,
                pid="P-OMN-1",
            ),
        ],
    )
    assert result.resolved is True
    assert result.project_id == UUID_A
    assert result.project_name is not None
    assert result.project_name.startswith("Sprint 2026-09-21")


def test_a_boundary_date_is_broken_by_completed_at() -> None:
    projects = [
        _project(
            "Sprint 2026-08-31 → 2026-09-07 (Beta)",
            "2026-08-31",
            "2026-09-07",
            "2026-09-07T15:58:41Z",
            UUID_B,
        ),
        _project("Sprint 2026-09-07 → 2026-09-14 (Beta)", "2026-09-07", "2026-09-14"),
    ]
    result = resolve("2026-09-07", projects)
    assert result.resolved is True
    assert result.project_id == UUID_A


@pytest.mark.parametrize(
    "projects",
    [
        [],
        [_project("Sprint 2026-09-14 → 2026-09-21 (Beta)", "2026-09-14", "2026-09-21")],
        [
            _project(
                "Sprint 2026-09-21 → 2026-09-28 (Beta)", "2026-09-21", "2026-09-28"
            ),
            _project(
                "Sprint 2026-09-24 → 2026-09-30 (Beta)",
                "2026-09-24",
                "2026-09-30",
                uuid=UUID_B,
            ),
        ],
        # the only containing sprint is a completed one, and it is not the only containing one
        [
            _project(
                "Sprint 2026-09-21 → 2026-09-28 (Beta)",
                "2026-09-21",
                "2026-09-28",
                "2026-09-27T00:00:00Z",
            ),
            _project(
                "Sprint 2026-09-24 → 2026-09-30 (Beta)",
                "2026-09-24",
                "2026-09-30",
                "2026-09-29T00:00:00Z",
                UUID_B,
            ),
        ],
    ],
)
def test_zero_or_several_survivors_refuse_to_pick(
    projects: list[dict[str, Any]],
) -> None:
    result = resolve(DATE, projects)
    assert result.resolved is False
    assert result.project_id is None
    assert result.residuals is not None
    assert "refusing to pick" in result.residuals[0]


def test_non_sprint_and_malformed_names_are_never_candidates() -> None:
    for name in (
        "2026-09-21 → 2026-09-28 (Beta)",
        "Sprint 2026-09-21 (Beta)",
        "Sprint 2026-09-21 → 2026-09-28 → 2026-10-05 (Beta)",
    ):
        assert (
            resolve(DATE, [_project(name, "2026-09-21", "2026-09-28")]).resolved
            is False
        )


def test_a_sprint_without_a_window_is_not_a_match() -> None:
    project = _project(
        "Sprint 2026-09-21 → 2026-09-28 (Beta)", "2026-09-21", "2026-09-28"
    )
    project["target_date"] = None
    assert resolve(DATE, [project]).resolved is False


def test_override_is_taken_as_given_and_a_completed_one_is_recorded() -> None:
    done = _project(
        "Sprint 2026-09-14 → 2026-09-21 (Beta)",
        "2026-09-14",
        "2026-09-21",
        "2026-09-21T10:00:00Z",
    )
    result = resolve(DATE, [done], project_override=UUID_A)
    assert (result.resolved, result.project_id) == (True, UUID_A)
    assert result.residuals is not None
    assert any("completed sprint" in r for r in result.residuals)
    unlisted = resolve(DATE, [], project_override=UUID_B)
    assert unlisted.project_name is None
    assert unlisted.residuals is not None
    assert any("not among the listed" in r for r in unlisted.residuals)


def test_override_by_short_id_is_refused() -> None:
    with pytest.raises(ValueError, match="36-character uuid"):
        resolve(DATE, [], project_override="P-OMN-28")


# --- plan_scope ---------------------------------------------------------------


def _ticket(tid: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": tid,
        "state_type": "started",
        "state_name": "In Progress",
        "occ_contract": True,
    }
    return {**base, **fields}


def plan(tickets: list[dict[str, Any]], **fields: Any) -> Any:
    return decide(
        kind="plan_scope", date=DATE, project_id=UUID_A, tickets=tickets, **fields
    )


def test_a_ticket_is_a_candidate_only_when_all_four_exclusions_clear() -> None:
    result = plan(
        [
            _ticket("OMN-1"),
            _ticket(
                "OMN-2", occ_contract=False, merged_pr_count=1, state_name="In Review"
            ),
            _ticket("OMN-3", has_children=True),
            _ticket("OMN-4", fenced=True, fence_reason="claimed by lane x"),
            _ticket("OMN-5", external=True),
            _ticket("OMN-6", occ_contract=False),
        ]
    )
    assert result.candidates == ["OMN-1", "OMN-2"]
    assert {n.id: n.why_not for n in result.non_candidates} == {
        "OMN-3": "parent (has children): the rollup owns its state",
        "OMN-4": "fenced: claimed by lane x",
        "OMN-5": "external collaborator: comment-only",
        "OMN-6": "no OCC contract and no merged product PR",
    }
    assert result.verified_nothing is False


def test_counts_overlap_and_are_not_disjoint() -> None:
    result = plan(
        [
            _ticket("OMN-1"),
            _ticket("OMN-2", state_name="In Review", fenced=True, has_children=True),
            _ticket("OMN-3", occ_contract=False, external=True),
        ]
    )
    counts = result.counts
    assert (counts.enumerated, counts.in_progress, counts.in_review) == (3, 2, 1)
    assert (counts.fenced, counts.parents, counts.external) == (1, 1, 1)
    assert counts.no_contract_no_merged_pr == 1
    assert (counts.occ_contract_present, counts.candidates) == (2, 1)
    assert counts.candidates_with_occ_contract == 1
    assert result.overlaps == [
        "OMN-2: fenced + parent",
        "OMN-3: external + no-contract-no-merged-pr",
    ]


def test_candidates_are_cut_into_chunks_of_the_given_size() -> None:
    tickets = [_ticket(f"OMN-{n}") for n in range(1, 8)]
    assert plan(tickets).chunks == [
        ["OMN-1", "OMN-2", "OMN-3", "OMN-4", "OMN-5"],
        ["OMN-6", "OMN-7"],
    ]
    assert plan(tickets, chunk_size=3).chunks == [
        ["OMN-1", "OMN-2", "OMN-3"],
        ["OMN-4", "OMN-5", "OMN-6"],
        ["OMN-7"],
    ]


def test_zero_candidates_is_a_valid_scope_that_says_so() -> None:
    result = plan([_ticket("OMN-1", fenced=True)])
    assert result.verified_nothing is True
    assert result.candidates == []
    assert result.chunks == []
    empty = plan([])
    assert empty.verified_nothing is True
    assert empty.counts.enumerated == 0


@pytest.mark.parametrize(
    ("tickets", "message"),
    [
        ([_ticket("OMN-1"), _ticket("OMN-1")], "duplicate ticket id: OMN-1"),
        ([_ticket("OMN-1", state_type="completed")], "not started: OMN-1"),
    ],
)
def test_plan_scope_refuses_an_input_it_could_misread_as_a_clean_scope(
    tickets: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        plan(tickets)


def test_plan_scope_refuses_a_short_sprint_id() -> None:
    with pytest.raises(ValueError, match="36-character uuid"):
        decide(kind="plan_scope", date=DATE, project_id="P-OMN-28", tickets=[])


# --- error chain: refusals before the handler runs ------------------------------


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"kind": "precheck", "date": DATE, "ledger_rows": []},
            "precheck requires clock_utc",
        ),
        (
            {"kind": "precheck", "date": DATE, "clock_utc": CLOCK},
            "precheck requires ledger_rows",
        ),
        (
            {
                "kind": "precheck",
                "date": DATE,
                "clock_utc": "yesterday",
                "ledger_rows": [],
            },
            "clock_utc must be",
        ),
        ({"kind": "resolve_sprint", "date": DATE}, "resolve_sprint requires projects"),
        (
            {"kind": "plan_scope", "date": DATE, "tickets": []},
            "plan_scope requires project_id",
        ),
        (
            {"kind": "plan_scope", "date": DATE, "project_id": UUID_A},
            "plan_scope requires tickets",
        ),
        (
            {
                "kind": "plan_scope",
                "date": "09-24",
                "project_id": UUID_A,
                "tickets": [],
            },
            "date must be YYYY-MM-DD",
        ),
        (
            {
                "kind": "plan_scope",
                "date": DATE,
                "project_id": UUID_A,
                "tickets": [],
                "force": True,
            },
            "plan_scope does not take force",
        ),
        (
            {"kind": "resolve_sprint", "date": DATE, "projects": [], "chunk_size": 2},
            "resolve_sprint does not take chunk_size",
        ),
        (
            {
                "kind": "plan_scope",
                "date": DATE,
                "project_id": UUID_A,
                "tickets": [],
                "chunk_size": 0,
            },
            "greater than or equal to 1",
        ),
        (
            {
                "kind": "plan_scope",
                "date": DATE,
                "project_id": UUID_A,
                "tickets": [{"id": "OMN-1"}],
            },
            "state_type",
        ),
        ({"kind": "bogus", "date": DATE}, "kind"),
        ({"kind": "resolve_sprint", "date": DATE, "projects": [], "extra": 1}, "extra"),
    ],
)
def test_error_chain_refuses_before_the_handler_runs(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelDodCloseoutDecisionRequest.model_validate(payload)


# --- golden chain over the in-memory bus ----------------------------------------


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_precheck_then_sprint_then_scope_over_the_bus(
    tmp_path: Path,
) -> None:
    pre = (
        await _run(
            tmp_path / "pre",
            {"kind": "precheck", "date": DATE, "clock_utc": CLOCK, "ledger_rows": []},
        )
    ).handler_result
    assert isinstance(pre, ModelDodCloseoutDecisionResult)
    assert pre.verdict is EnumPrecheckVerdict.RUN

    sprint = (
        await _run(
            tmp_path / "sprint",
            {
                "kind": "resolve_sprint",
                "date": DATE,
                "projects": [
                    _project(
                        "Sprint 2026-09-21 → 2026-09-28 (Beta)",
                        "2026-09-21",
                        "2026-09-28",
                    )
                ],
            },
        )
    ).handler_result
    assert isinstance(sprint, ModelDodCloseoutDecisionResult)
    assert sprint.project_id == UUID_A

    scope = (
        await _run(
            tmp_path / "scope",
            {
                "kind": "plan_scope",
                "date": DATE,
                "project_id": sprint.project_id,
                "tickets": [_ticket("OMN-1"), _ticket("OMN-2", fenced=True)],
            },
        )
    ).handler_result
    assert isinstance(scope, ModelDodCloseoutDecisionResult)
    assert scope.chunks == [["OMN-1"]]
    assert (
        ModelDodCloseoutDecisionResult.model_validate_json(scope.model_dump_json())
        == scope
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    invalid = await _run(tmp_path / "invalid", {"kind": "precheck", "date": DATE})
    assert invalid.handler_result is None
    refused_dir = tmp_path / "refused"
    refused_dir.mkdir()
    payload = {
        "kind": "plan_scope",
        "date": DATE,
        "project_id": UUID_A,
        "tickets": [_ticket("OMN-1", state_type="completed")],
    }
    input_path = refused_dir / "request.json"
    input_path.write_text(json.dumps(payload))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=refused_dir / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None


# --- flip_decision --------------------------------------------------------------

FLIPPABLE: dict[str, Any] = {
    "ticket_id": "OMN-1",
    "total_checks": 6,
    "verified_count": 5,
    "non_probative_count": 1,
    "behavior_proving_count": 2,
    "criteria": ["AC1", "AC2", "AC3"],
    "bound_criteria": ["AC1", "AC2", "AC3"],
    "all_prs_merged": True,
}


def flip(**overrides: Any) -> Any:
    return decide(kind="flip_decision", date=DATE, **{**FLIPPABLE, **overrides})


def test_every_conjunct_met_flips() -> None:
    result = flip()
    assert result.action is EnumCloseoutAction.FLIPPED_DONE
    assert (result.predicate_met, result.unmet, result.primary_blocker) == (
        True,
        [],
        None,
    )
    assert flip(released="released").action is EnumCloseoutAction.FLIPPED_DONE
    assert flip(released="not-applicable").action is EnumCloseoutAction.FLIPPED_DONE


@pytest.mark.parametrize(
    ("overrides", "blocker"),
    [
        ({"all_prs_merged": False}, "pr:unmerged"),
        ({"criteria": [], "bound_criteria": []}, "ac:unparseable"),
        ({"bound_criteria": ["AC1", "AC2"]}, "ac:unbound"),
        # three checks verified against three criteria says nothing about which one each covers
        ({"bound_criteria": ["AC1", "AC2", "AC9"]}, "ac:unbound"),
        ({"total_checks": 7}, "checks:tally"),
        (
            {
                "total_checks": 6,
                "verified_count": 3,
                "failed_count": 2,
                "non_probative_count": 1,
            },
            "checks:tally",
        ),
        # a green tally over zero checks is an arithmetic identity, not evidence
        (
            {"total_checks": 0, "verified_count": 0, "non_probative_count": 0},
            "checks:tally",
        ),
        ({"behavior_proving_count": 0}, "behavior:none"),
        ({"prior_reversal": True}, "reverted:unchanged"),
    ],
)
def test_one_unmet_conjunct_holds_and_names_its_class(
    overrides: dict[str, Any], blocker: str
) -> None:
    result = flip(**overrides)
    assert result.action is EnumCloseoutAction.HELD_GAP
    assert result.predicate_met is False
    assert result.primary_blocker == blocker
    assert result.unmet is not None
    assert blocker in result.unmet


def test_the_unbound_criteria_are_named() -> None:
    result = flip(bound_criteria=["AC2"])
    assert result.unbound_criteria == ["AC1", "AC3"]
    assert flip().unbound_criteria == []


def test_a_reversal_releases_only_when_a_check_outcome_changed() -> None:
    assert flip(prior_reversal=True, outcome_changed_since_reversal=True).action is (
        EnumCloseoutAction.FLIPPED_DONE
    )


def test_several_unmet_conjuncts_are_all_listed_with_the_first_as_primary() -> None:
    result = flip(all_prs_merged=False, bound_criteria=[], behavior_proving_count=0)
    assert result.unmet == ["pr:unmerged", "ac:unbound", "behavior:none"]
    assert result.primary_blocker == "pr:unmerged"


def test_merged_unreleased_is_its_own_hold_not_a_gap() -> None:
    result = flip(released="merged-unreleased")
    assert result.action is EnumCloseoutAction.HELD_MERGED_UNRELEASED
    assert (result.predicate_met, result.primary_blocker) == (
        True,
        "released:merged-unreleased",
    )


def test_an_indeterminate_release_read_is_never_a_flip() -> None:
    result = flip(released="indeterminate")
    assert result.action is EnumCloseoutAction.HELD_GAP
    assert (result.predicate_met, result.primary_blocker) == (
        True,
        "released:indeterminate",
    )


def test_an_unmet_conjunct_beats_the_release_state() -> None:
    result = flip(released="merged-unreleased", bound_criteria=[])
    assert result.action is EnumCloseoutAction.HELD_GAP
    assert result.primary_blocker == "ac:unbound"


# --- report ---------------------------------------------------------------------

SCOPE_COUNTS = {
    "enumerated": 79,
    "in_progress": 40,
    "in_review": 39,
    "fenced": 37,
    "parents": 11,
    "external": 15,
    "no_contract_no_merged_pr": 6,
    "occ_contract_present": 60,
    "candidates": 7,
    "candidates_with_occ_contract": 7,
}


def _result(tid: str, action: str, **fields: Any) -> dict[str, Any]:
    return {"id": tid, "action": action, **fields}


def build_report(chunks: list[dict[str, Any]], **fields: Any) -> Any:
    return decide(
        kind="report",
        date=DATE,
        project_id=UUID_A,
        project_name="Sprint 2026-09-21 → 2026-09-28 (Beta)",
        scope_counts=SCOPE_COUNTS,
        chunk_results=chunks,
        plan_comparison="The P1 order still matches. The dominant class has a row.",
        not_done="No product code. No parent written.",
        released_positive_control="9f034fd6 is contained in v1.2.3",
        **fields,
    )


MIXED_CHUNKS: list[dict[str, Any]] = [
    {
        "chunk": 0,
        "tickets": ["OMN-1", "OMN-2", "OMN-3", "OMN-4", "OMN-5"],
        "audited": True,
        "reverted": ["OMN-2"],
        "audit_notes": "re-derived OMN-1 and OMN-2",
        "results": [
            _result(
                "OMN-1",
                "flipped-done",
                product_pr="o/r#1",
                merge_sha="aaa111",
                receipt="drift/dod_receipts/OMN-1 PASS",
                behavior_proving_count=2,
                predicate_met=True,
            ),
            _result(
                "OMN-2",
                "flipped-done",
                product_pr="o/r#2",
                merge_sha="bbb222",
                receipt="r",
                behavior_proving_count=1,
                predicate_met=True,
            ),
            _result(
                "OMN-3",
                "held-gap",
                unmet_check="AC2 bound to no check",
                primary_blocker="ac:unbound",
                tooling_blockers=["CHECK_BUDGET_EXCEEDED"],
                behavior_proving_count=0,
                predicate_met=False,
            ),
            _result(
                "OMN-4",
                "held-merged-unreleased",
                repo="omnibase_core",
                merge_sha="ccc333",
                tag_lookup="no v* tag contains it",
                index_read="0.47.1 served",
                release_ticket="OMN-9",
                behavior_proving_count=1,
                predicate_met=True,
            ),
            _result(
                "OMN-5", "held-external", unmet_check="external collaborator ticket"
            ),
        ],
    },
    {
        "chunk": 1,
        "tickets": ["OMN-6", "OMN-7"],
        "audited": True,
        "results": [
            _result("OMN-6", "corrected-state"),
            _result(
                "OMN-7",
                "held-gap",
                unmet_check="released read unreachable",
                primary_blocker="released:indeterminate",
                tooling_blockers=["CHECK_BUDGET_EXCEEDED"],
                behavior_proving_count=3,
                predicate_met=True,
            ),
        ],
    },
]


def test_counts_are_derived_from_the_results_and_a_reverted_flip_is_not_a_flip() -> (
    None
):
    result = build_report(MIXED_CHUNKS)
    counts = result.closeout_counts
    assert (
        counts.flipped,
        counts.held,
        counts.merged_unreleased,
        counts.corrected,
        counts.reverted,
    ) == (1, 3, 1, 1, 1)
    assert (counts.fenced, counts.parents, counts.external) == (37, 11, 15)
    assert (counts.candidates, counts.enumerated) == (7, 79)
    assert result.verified_nothing is False
    assert result.report_path == "beta/tracking/2026-09-24-dod-closeout-sweep.md"


def test_primary_histogram_sums_to_held_and_tooling_is_occurrences() -> None:
    result = build_report(MIXED_CHUNKS)
    primary = {r.check_class: r.count for r in result.primary_histogram}
    assert primary == {
        "ac:unbound": 1,
        "external:comment-only": 1,
        "released:indeterminate": 1,
    }
    assert sum(primary.values()) == result.closeout_counts.held
    tooling = {r.check_class: r.count for r in result.tooling_histogram}
    assert tooling == {
        "CHECK_BUDGET_EXCEEDED": 2,
        "released:indeterminate": 1,
        "released:merged-unreleased": 1,
    }
    assert (result.behavior_proving_positive, result.predicate_satisfied) == (4, 4)


def test_a_held_ticket_with_no_class_is_unclassified_not_dropped() -> None:
    result = build_report(
        [
            {
                "chunk": 0,
                "tickets": ["OMN-1"],
                "audited": True,
                "results": [_result("OMN-1", "held-gap", unmet_check="x")],
            }
        ]
    )
    assert [(r.check_class, r.count) for r in result.primary_histogram] == [
        ("unclassified", 1)
    ]


def test_a_dropped_chunk_and_unadjudicated_tickets_are_named() -> None:
    chunks = [
        MIXED_CHUNKS[0],
        {"chunk": 1, "tickets": ["OMN-6", "OMN-7"], "dropped": True},
    ]
    result = build_report(chunks)
    assert result.dropped_chunks == [1]
    assert result.unadjudicated_tickets == ["OMN-6", "OMN-7"]
    assert result.report_text is not None
    assert "## DROPPED CHUNKS" in result.report_text
    assert "OMN-6, OMN-7" in result.report_text
    assert "DROPPED, not adjudicated" in result.report_text


def test_a_ticket_missing_from_a_live_chunk_is_unadjudicated_too() -> None:
    chunks = [
        {
            "chunk": 0,
            "tickets": ["OMN-1", "OMN-2"],
            "results": [_result("OMN-1", "no-change")],
        }
    ]
    result = build_report(chunks)
    assert result.unadjudicated_tickets == ["OMN-2"]
    assert result.report_text is not None
    assert "NOT AUDITED" in result.report_text


def test_zero_candidates_report_opens_with_the_marker_and_the_exclusions() -> None:
    scope = {**SCOPE_COUNTS, "candidates": 0, "candidates_with_occ_contract": 0}
    result = decide(
        kind="report",
        date=DATE,
        project_id=UUID_A,
        project_name="Sprint X",
        scope_counts=scope,
        chunk_results=[],
    )
    assert result.verified_nothing is True
    assert result.report_text is not None
    first, second, third = result.report_text.split("\n")[:3]
    assert first == "VERIFIED NOTHING: zero candidates"
    assert UUID_A in second
    assert "fenced 37, parents 11, external 15, no contract and no merged PR 6" in third
    live = build_report(MIXED_CHUNKS)
    assert live.report_text is not None
    assert not live.report_text.startswith("VERIFIED NOTHING")


def test_the_flipped_table_names_a_missing_receipt_as_a_defect() -> None:
    result = build_report(
        [
            {
                "chunk": 0,
                "tickets": ["OMN-1"],
                "audited": True,
                "results": [
                    _result(
                        "OMN-1", "flipped-done", product_pr="o/r#1", merge_sha="aaa"
                    )
                ],
            }
        ]
    )
    assert result.report_text is not None
    assert "NONE CITED - defect" in result.report_text
    assert "NONE CITED" not in (build_report(MIXED_CHUNKS).report_text or "")


def test_an_empty_merged_unreleased_section_needs_its_positive_control() -> None:
    chunks = [MIXED_CHUNKS[1]]
    proven = build_report(chunks)
    assert proven.report_text is not None
    assert "Positive control: 9f034fd6 is contained in v1.2.3" in proven.report_text
    unproven = decide(
        kind="report",
        date=DATE,
        project_id=UUID_A,
        scope_counts=SCOPE_COUNTS,
        chunk_results=chunks,
        released_probe_run=False,
    )
    assert unproven.report_text is not None
    assert "an unrun probe is not a zero" in unproven.report_text


def test_dry_mode_is_said_in_the_header() -> None:
    dry = build_report(MIXED_CHUNKS, apply=False)
    assert dry.report_text is not None
    assert "mode: dry (no state written)" in dry.report_text
    assert "mode: apply" in (build_report(MIXED_CHUNKS).report_text or "")


def test_the_rendered_report_is_conformant_and_under_the_line_cap() -> None:
    result = build_report(MIXED_CHUNKS)
    assert result.checks_failed == []
    assert result.report_lines is not None
    assert 0 < result.report_lines <= 150
    assert result.report_text is not None
    assert result.report_lines == len(result.report_text.rstrip("\n").split("\n"))


def test_a_report_over_the_cap_is_flagged_not_truncated() -> None:
    tickets = [f"OMN-{n}" for n in range(1, 181)]
    chunks = [
        {
            "chunk": 0,
            "tickets": tickets,
            "audited": True,
            "results": [
                _result(t, "held-gap", unmet_check="x", primary_blocker="ac:unbound")
                for t in tickets
            ],
        }
    ]
    result = build_report(chunks)
    assert result.checks_failed is not None
    assert "report-lines>150" in result.checks_failed
    assert result.report_text is not None
    assert "OMN-180" in result.report_text


def test_terminal_cells_are_one_key_value_each_and_cite_the_report() -> None:
    result = build_report(MIXED_CHUNKS, friction="see FRICTION row of lane x")
    cells = result.terminal_cells
    assert cells is not None
    for cell in cells:
        assert re.fullmatch(r"[a-z_]+=[^|\n]+", cell), cell
    keys = [c.split("=", 1)[0] for c in cells]
    assert keys == [
        "lane",
        "date",
        "friction",
        "sprint",
        "enumerated",
        "candidates",
        "flipped",
        "held",
        "merged_unreleased",
        "corrected",
        "reverted",
        "fenced",
        "parents",
        "external",
    ]
    by_key = dict(c.split("=", 1) for c in cells)
    assert by_key["lane"] == "dod-closeout-sweep"
    assert by_key["friction"] == "see FRICTION row of lane x"
    assert (
        by_key["flipped"]
        == "1 [cite: beta/tracking/2026-09-24-dod-closeout-sweep.md §2]"
    )


def test_the_rendered_report_and_cells_are_what_the_precheck_accepts_as_delivered() -> (
    None
):
    result = build_report(MIXED_CHUNKS)
    assert result.report_text is not None
    assert result.terminal_cells is not None
    row = " | ".join(["2026-09-24T15:00:00Z", "TERMINAL", *result.terminal_cells])
    verdict = precheck(
        [row], report_text=result.report_text, report_commit_sha="abc123"
    )
    assert verdict.verdict is EnumPrecheckVerdict.ALREADY_DELIVERED


def test_report_refuses_a_friction_that_would_break_the_ledger_row() -> None:
    for bad in ("a | b", "a\nb"):
        with pytest.raises(ValidationError, match="friction is one ledger cell"):
            ModelDodCloseoutDecisionRequest.model_validate(
                {
                    "kind": "report",
                    "date": DATE,
                    "project_id": UUID_A,
                    "scope_counts": SCOPE_COUNTS,
                    "chunk_results": [],
                    "friction": bad,
                }
            )


@pytest.mark.parametrize(
    ("chunks", "message"),
    [
        (
            [
                {
                    "chunk": 0,
                    "tickets": ["OMN-1"],
                    "results": [_result("OMN-1", "no-change")],
                },
                {
                    "chunk": 1,
                    "tickets": ["OMN-1"],
                    "results": [_result("OMN-1", "no-change")],
                },
            ],
            "more than one result",
        ),
        (
            [{"chunk": 0, "tickets": ["OMN-1"]}, {"chunk": 0, "tickets": ["OMN-2"]}],
            "chunk number twice",
        ),
        ([{"chunk": 0, "tickets": []}], "at least 1"),
        (
            [
                {
                    "chunk": 0,
                    "tickets": ["OMN-1"],
                    "results": [_result("OMN-1", "bogus")],
                }
            ],
            "action",
        ),
    ],
)
def test_report_refuses_inconsistent_chunk_results(
    chunks: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelDodCloseoutDecisionRequest.model_validate(
            {
                "kind": "report",
                "date": DATE,
                "project_id": UUID_A,
                "scope_counts": SCOPE_COUNTS,
                "chunk_results": chunks,
            }
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "kind": "flip_decision",
                "date": DATE,
                "total_checks": 1,
                "all_prs_merged": True,
            },
            "flip_decision requires ticket_id",
        ),
        (
            {
                "kind": "flip_decision",
                "date": DATE,
                "ticket_id": "OMN-1",
                "all_prs_merged": True,
            },
            "flip_decision requires total_checks",
        ),
        (
            {
                "kind": "flip_decision",
                "date": DATE,
                "ticket_id": "OMN-1",
                "total_checks": 1,
            },
            "flip_decision requires all_prs_merged",
        ),
        (
            {
                "kind": "flip_decision",
                "date": DATE,
                "ticket_id": "OMN-1",
                "total_checks": -1,
                "all_prs_merged": True,
            },
            "greater than or equal to 0",
        ),
        (
            {
                "kind": "flip_decision",
                "date": DATE,
                "ticket_id": "OMN-1",
                "total_checks": 1,
                "all_prs_merged": True,
                "released": "maybe",
            },
            "released",
        ),
        (
            {
                "kind": "flip_decision",
                "date": DATE,
                "ticket_id": "OMN-1",
                "total_checks": 1,
                "all_prs_merged": True,
                "apply": False,
            },
            "flip_decision does not take apply",
        ),
        (
            {
                "kind": "precheck",
                "date": DATE,
                "clock_utc": CLOCK,
                "ledger_rows": [],
                "criteria": ["AC1"],
            },
            "precheck does not take criteria",
        ),
        (
            {"kind": "report", "date": DATE, "project_id": UUID_A, "chunk_results": []},
            "report requires scope_counts",
        ),
        (
            {
                "kind": "report",
                "date": DATE,
                "project_id": UUID_A,
                "scope_counts": SCOPE_COUNTS,
            },
            "report requires chunk_results",
        ),
    ],
)
def test_error_chain_refuses_flip_and_report_requests(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelDodCloseoutDecisionRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_golden_chain_flip_then_report_over_the_bus(tmp_path: Path) -> None:
    flipped = (
        await _run(
            tmp_path / "flip",
            {
                "kind": "flip_decision",
                "date": DATE,
                **FLIPPABLE,
                "released": "merged-unreleased",
            },
        )
    ).handler_result
    assert isinstance(flipped, ModelDodCloseoutDecisionResult)
    assert flipped.action is EnumCloseoutAction.HELD_MERGED_UNRELEASED
    done = (
        await _run(
            tmp_path / "report",
            {
                "kind": "report",
                "date": DATE,
                "project_id": UUID_A,
                "scope_counts": SCOPE_COUNTS,
                "chunk_results": MIXED_CHUNKS,
                "plan_comparison": "Matches.",
                "not_done": "Nothing else.",
            },
        )
    ).handler_result
    assert isinstance(done, ModelDodCloseoutDecisionResult)
    assert done.checks_failed == []
    assert done.closeout_counts is not None
    assert done.closeout_counts.flipped == 1
    assert (
        ModelDodCloseoutDecisionResult.model_validate_json(done.model_dump_json())
        == done
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_refuses_an_inconsistent_report(
    tmp_path: Path,
) -> None:
    runtime = await _run(
        tmp_path / "bad",
        {
            "kind": "report",
            "date": DATE,
            "project_id": UUID_A,
            "scope_counts": SCOPE_COUNTS,
            "chunk_results": [],
            "friction": "a | b",
        },
    )
    assert runtime.handler_result is None
