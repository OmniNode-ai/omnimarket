# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20675: the node decides what the live closer decides.

The live closer (the dod-closeout-sweep workflow in the internal tooling repository) decides
in code: which sprint is live, whether a bound check may go to the acceptor, whether two
lanes are one actor, which tickets a bounded run examines, and whether a ticket is Done. The
node must make the same decision on the same input, so the closer can ask it over the bus.

``fixtures/closer_decisions.json`` is a recorded corpus: each entry is a request and the
answer the closer's own pure block (``refusalOf``, ``sameActor``, ``selectCandidates`` with
``withTextState`` and ``chunkList``, ``decideTicket`` with ``readDodVerify`` and
``openComment``) gave for it under node. The sprint rule has no recorded entries: the closer
resolves it in prose, so its cases are written out below from that prose.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import omnimarket.nodes.node_dod_closeout_sweep_compute as node_package
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    HandlerDodCloseoutSweep,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    ModelDodCloseoutDecisionRequest,
    ModelDodCloseoutDecisionResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
CORPUS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "closer_decisions.json").read_text()
)
HANDLER = HandlerDodCloseoutSweep()
DATE = "2026-10-09"
UUID_A = "a7530301-012d-4bd8-9c67-b24fbe21d93f"
UUID_B = "11111111-2222-3333-4444-555555555555"
UUID_C = "22222222-3333-4444-5555-666666666666"


def decide(**fields: Any) -> ModelDodCloseoutDecisionResult:
    return HANDLER.handle(ModelDodCloseoutDecisionRequest.model_validate(fields))


# --- recorded corpus: the node answers as the closer did --------------------------


def _expected(entry: dict[str, Any]) -> dict[str, Any]:
    expected = dict(entry["expected"])
    if entry["kind"] == "decide_ticket":
        expected["ticket_unmet"] = expected.pop("unmet")
    return expected


@pytest.mark.parametrize(
    "index", range(len(CORPUS)), ids=lambda i: f"{CORPUS[i]['kind']}-{i}"
)
def test_the_node_answers_each_recorded_input_as_the_closer_did(index: int) -> None:
    entry = CORPUS[index]
    result = decide(kind=entry["kind"], date=DATE, **entry["request"])
    dumped = result.model_dump(mode="json")
    expected = _expected(entry)
    assert {key: dumped[key] for key in expected} == expected


def test_the_corpus_is_not_vacuous() -> None:
    kinds = Counter(entry["kind"] for entry in CORPUS)
    assert set(kinds) == {
        "refuse_binding",
        "check_identities",
        "select_candidates",
        "decide_ticket",
    }
    refusals = {
        entry["expected"]["refusal_class"]
        for entry in CORPUS
        if entry["kind"] == "refuse_binding"
    }
    assert None in refusals
    assert len(refusals - {None}) == 15
    assert {
        entry["expected"]["decision"]
        for entry in CORPUS
        if entry["kind"] == "decide_ticket"
    } == {"open", "done"}
    assert {
        entry["expected"]["same_actor"]
        for entry in CORPUS
        if entry["kind"] == "check_identities"
    } == {True, False}


# --- the sprint rule: the closer dropped the (Beta) suffix ------------------------


def _project(
    name: str,
    start: str | None,
    end: str | None,
    *,
    uuid: str = UUID_A,
    pid: str = "P-OMN-28",
    done: str | None = None,
) -> dict[str, Any]:
    return {
        "id": pid,
        "uuid": uuid,
        "name": name,
        "start_date": start,
        "target_date": end,
        "completed_at": done,
    }


def resolve(projects: list[dict[str, Any]], date: str = DATE) -> Any:
    return decide(kind="resolve_sprint", date=date, projects=projects)


@pytest.mark.parametrize(
    "name",
    [
        "Sprint 2026-10-05 -> 2026-10-11",
        "Sprint 2026-10-05 -> 2026-10-11: M4 End-to-End Part 1",
        "Sprint 2026-10-05 → 2026-10-11 (Beta)",
        "Sprint 2026-10-5 -> 2026-10-11 M4 End-to-End Part 1",
        "Sprint 2026-10-5 to 2026-10-11",
    ],
)
def test_a_sprint_resolves_with_or_without_a_beta_suffix_or_zero_padding(
    name: str,
) -> None:
    result = resolve([_project(name, "2026-10-05", "2026-10-11")])
    assert result.resolved is True
    assert result.project_id == UUID_A


def test_the_window_comes_from_the_project_fields_never_from_the_name() -> None:
    wrong_dates_in_name = _project(
        "Sprint 2026-09-01 -> 2026-09-07", "2026-10-05", "2026-10-11"
    )
    assert resolve([wrong_dates_in_name]).resolved is True
    assert resolve([wrong_dates_in_name], date="2026-09-03").resolved is False


@pytest.mark.parametrize(
    "name",
    [
        "Platform roadmap",
        "2026-10-05 -> 2026-10-11",
        "Sprint 2026-10-05",
        "Sprint 2026-10-05 -> 2026-10-11 -> 2026-10-18",
        "sprint 2026-10-05 -> 2026-10-11",
    ],
)
def test_a_name_that_is_not_a_two_date_sprint_is_never_a_candidate(name: str) -> None:
    assert resolve([_project(name, "2026-10-05", "2026-10-11")]).resolved is False


def test_the_previous_sprint_is_the_latest_one_ending_on_or_before_the_start() -> None:
    result = resolve(
        [
            _project("Sprint 2026-10-05 -> 2026-10-11", "2026-10-05", "2026-10-11"),
            _project(
                "Sprint 2026-09-28 -> 2026-10-04: M3",
                "2026-09-28",
                "2026-10-04",
                uuid=UUID_B,
                pid="P-OMN-27",
                done="2026-10-05T00:00:00Z",
            ),
            _project(
                "Sprint 2026-09-14 -> 2026-09-20",
                "2026-09-14",
                "2026-09-20",
                uuid=UUID_C,
                pid="P-OMN-26",
            ),
            _project(
                "Sprint 2026-10-12 -> 2026-10-18",
                "2026-10-12",
                "2026-10-18",
                uuid="33333333-4444-5555-6666-777777777777",
                pid="P-OMN-29",
            ),
        ]
    )
    assert result.project_id == UUID_A
    assert result.previous_project_id == UUID_B
    assert result.previous_project_name is not None
    assert result.previous_project_name.startswith("Sprint 2026-09-28")


def test_two_previous_sprints_sharing_the_latest_end_are_both_returned_and_said() -> (
    None
):
    result = resolve(
        [
            _project("Sprint 2026-10-05 -> 2026-10-11", "2026-10-05", "2026-10-11"),
            _project(
                "Sprint 2026-09-28 -> 2026-10-04 A",
                "2026-09-28",
                "2026-10-04",
                uuid=UUID_B,
                pid="P-OMN-27",
            ),
            _project(
                "Sprint 2026-09-30 -> 2026-10-04 B",
                "2026-09-30",
                "2026-10-04",
                uuid=UUID_C,
                pid="P-OMN-30",
            ),
        ]
    )
    assert result.previous_project_id == f"{UUID_B},{UUID_C}"
    assert result.residuals is not None
    assert any("share the latest end" in line for line in result.residuals)


def test_no_previous_sprint_is_the_empty_string_and_an_unresolved_date_has_none() -> (
    None
):
    only = resolve(
        [_project("Sprint 2026-10-05 -> 2026-10-11", "2026-10-05", "2026-10-11")]
    )
    assert only.previous_project_id == ""
    assert only.previous_project_name == ""
    none = resolve([_project("Platform roadmap", "2026-10-05", "2026-10-11")])
    assert none.resolved is False
    assert none.previous_project_id is None


# --- the closer's own refusals, by example ----------------------------------------


def test_a_symbol_grep_is_refused_and_a_test_id_on_merged_dev_is_not() -> None:
    grep = decide(
        kind="refuse_binding",
        date=DATE,
        check={
            "label": "AC1",
            "kind": "command",
            "expect": "found",
            "command": "grep -rn handle src",
            "cwd": "omnimarket",
            "ref": "origin/dev",
        },
    )
    assert grep.refused is True
    assert grep.refusal_class == "symbol-grep-only"
    assert grep.reason is not None
    assert grep.reason.startswith("binding refused (symbol-grep-only):")
    test_id = decide(
        kind="refuse_binding",
        date=DATE,
        check={
            "label": "AC1",
            "kind": "test",
            "expect": "passes",
            "selector": "tests/test_x.py::test_a",
            "repo": "OmniNode-ai/omnimarket",
            "ref": "origin/dev",
        },
    )
    assert test_id.refused is False
    assert test_id.refusal_class is None
    assert test_id.reason is None


def test_an_absent_check_is_refused_and_the_criterion_label_stands_in() -> None:
    absent = decide(kind="refuse_binding", date=DATE)
    assert absent.refusal_class == "no-check"
    unlabelled = {
        "kind": "test",
        "expect": "e",
        "selector": "a.py::t",
        "repo": "omnimarket",
        "ref": "origin/dev",
    }
    assert (
        decide(kind="refuse_binding", date=DATE, check=unlabelled).refusal_class
        == "no-label"
    )
    assert (
        decide(
            kind="refuse_binding",
            date=DATE,
            check=unlabelled,
            criterion_label="AC4",
        ).refused
        is False
    )


def test_a_lane_never_accepts_its_own_binding_and_a_blank_actor_names_nobody() -> None:
    def same(proposed_by: str, accepted_by: str) -> bool | None:
        return decide(
            kind="check_identities",
            date=DATE,
            proposed_by=proposed_by,
            accepted_by=accepted_by,
        ).same_actor

    assert same("lane=bind-c0", "lane=accept-c0") is False
    assert same("lane=bind-c0", "LANE=BIND-C0") is True
    assert same("alice@example.com", "alice@example.com") is True
    assert same("", "lane=accept-c0") is True
    assert same("lane=bind-c0", "") is True


def test_a_ticket_is_done_only_when_every_criterion_is_accepted_by_another_and_verified() -> (
    None
):
    receipt = json.dumps(
        {
            "result_model": "omnimarket.nodes.node_dod_verify.models.model_dod_verify_state.ModelDodVerifyState",
            "result": {
                "status": "verified",
                "total_checks": 1,
                "verified_count": 1,
                "checks": [
                    {"evidence_id": "e1", "binds_ac": ["AC1"], "status": "verified"}
                ],
            },
        }
    )
    ac = {
        "label": "AC1",
        "proposed": True,
        "accepted": True,
        "proposed_by": "lane=bind-c0",
        "accepted_by": "lane=accept-c0",
    }
    done = decide(
        kind="decide_ticket",
        date=DATE,
        ticket_id="OMN-1",
        acs=[ac],
        dod_verify_receipt=receipt,
    )
    assert done.decision == "done"
    assert done.ticket_unmet == []
    assert done.comment_text is None
    selfish = decide(
        kind="decide_ticket",
        date=DATE,
        ticket_id="OMN-1",
        acs=[{**ac, "accepted_by": "lane=bind-c0"}],
        dod_verify_receipt=receipt,
        run_key="2026-10-09T0845Z",
    )
    assert selfish.decision == "open"
    assert selfish.ticket_unmet is not None
    assert [u.reason for u in selfish.ticket_unmet] == [
        "binding was not accepted by a lane other than its author"
    ]
    assert selfish.comment_text is not None
    assert selfish.comment_text.startswith("actor: dod-closeout-sweep (sonnet)\n")


def test_a_bounded_run_examines_the_least_recently_examined_and_defers_the_rest() -> (
    None
):
    result = decide(
        kind="select_candidates",
        date=DATE,
        records=[
            {"id": "OMN-3", "last_closeout_comment_at": "2026-10-08T10:00:00Z"},
            {"id": "OMN-1", "last_closeout_comment_at": "2026-10-09T10:00:00Z"},
            {"id": "OMN-2"},
            {"id": "OMN-4", "last_closeout_comment_at": "2026-10-07T10:00:00Z"},
        ],
        max_candidates=3,
        chunk_size=2,
    )
    assert result.selected == ["OMN-2", "OMN-4", "OMN-3"]
    assert result.deferred == ["OMN-1"]
    assert result.held == []
    assert result.chunks == [["OMN-2", "OMN-4"], ["OMN-3"]]


def test_a_ticket_whose_text_has_not_changed_since_its_amendment_hold_is_not_selected() -> (
    None
):
    text_state = json.dumps(
        [
            {
                "id": "OMN-1",
                "text_sha": "0123456789abcdef",
                "held_text_sha": "0123456789abcdef",
            },
            {
                "id": "OMN-2",
                "text_sha": "fedcba9876543210",
                "held_text_sha": "0123456789abcdef",
            },
        ]
    )
    records = [{"id": "OMN-1"}, {"id": "OMN-2"}, {"id": "OMN-3"}]
    result = decide(
        kind="select_candidates",
        date=DATE,
        records=records,
        text_state=text_state,
        max_candidates=5,
    )
    assert result.held == ["OMN-1"]
    assert result.selected == ["OMN-2", "OMN-3"]
    unread = decide(
        kind="select_candidates",
        date=DATE,
        records=records,
        text_state="not json",
        max_candidates=5,
    )
    assert unread.held == []
    assert unread.selected == ["OMN-1", "OMN-2", "OMN-3"]


def test_an_amendment_the_text_must_change_for_records_its_hash_in_the_comment() -> (
    None
):
    unprovable = {
        "label": "AC1",
        "proposed": True,
        "accepted": False,
        "reason": "false at origin/dev",
        "amendment": "state an observable outcome",
    }
    sha = "0123456789abcdef"
    held = decide(
        kind="decide_ticket",
        date=DATE,
        ticket_id="OMN-1",
        acs=[unprovable],
        text_sha=sha,
        run_key="2026-10-09T0845Z",
    )
    assert held.needs_amendment is True
    assert held.signature is not None
    assert held.signature.endswith(f"text={sha}")
    assert held.comment_text is not None
    assert f"amendment-text-sha={sha}" in held.comment_text
    plain = decide(
        kind="decide_ticket",
        date=DATE,
        ticket_id="OMN-1",
        acs=[{**unprovable, "amendment": ""}],
        text_sha=sha,
    )
    assert plain.needs_amendment is False
    assert plain.signature is not None
    assert "text=" not in plain.signature


# --- error chain: refusals before the handler runs --------------------------------


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"kind": "decide_ticket", "date": DATE, "acs": []},
            "decide_ticket requires ticket_id",
        ),
        (
            {"kind": "decide_ticket", "date": DATE, "ticket_id": "OMN-1"},
            "decide_ticket requires acs",
        ),
        (
            {"kind": "select_candidates", "date": DATE, "records": []},
            "select_candidates requires max_candidates",
        ),
        (
            {"kind": "select_candidates", "date": DATE, "max_candidates": 3},
            "select_candidates requires records",
        ),
        (
            {
                "kind": "select_candidates",
                "date": DATE,
                "records": [],
                "max_candidates": 0,
            },
            "greater than or equal to 1",
        ),
        (
            {
                "kind": "select_candidates",
                "date": DATE,
                "records": [{"id": "OMN-1", "bogus": 1}],
                "max_candidates": 3,
            },
            "bogus",
        ),
        (
            {"kind": "check_identities", "date": DATE, "chunk_size": 2},
            "check_identities does not take chunk_size",
        ),
        (
            {"kind": "refuse_binding", "date": DATE, "proposed_by": "lane=a"},
            "refuse_binding does not take proposed_by",
        ),
        (
            {"kind": "resolve_sprint", "date": DATE, "projects": [], "acs": []},
            "resolve_sprint does not take acs",
        ),
        (
            {
                "kind": "decide_ticket",
                "date": DATE,
                "ticket_id": "OMN-1",
                "acs": [{"label": "AC1", "bogus": True}],
            },
            "bogus",
        ),
    ],
)
def test_error_chain_refuses_the_new_kinds_before_the_handler_runs(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelDodCloseoutDecisionRequest.model_validate(payload)


# --- golden chain over the in-memory bus ------------------------------------------


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
@pytest.mark.parametrize(
    "kind", ["refuse_binding", "check_identities", "select_candidates", "decide_ticket"]
)
async def test_golden_chain_each_new_kind_answers_over_the_bus_as_the_handler_does(
    tmp_path: Path, kind: str
) -> None:
    entry = next(e for e in CORPUS if e["kind"] == kind)
    payload = {"kind": kind, "date": DATE, **entry["request"]}
    ran = await _run(tmp_path / kind, payload)
    assert isinstance(ran.handler_result, ModelDodCloseoutDecisionResult)
    assert ran.handler_result == decide(**payload)
    dumped = ran.handler_result.model_dump(mode="json")
    expected = _expected(entry)
    assert {key: dumped[key] for key in expected} == expected
