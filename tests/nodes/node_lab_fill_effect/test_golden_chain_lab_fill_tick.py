# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: one lab-fill tick dispatches approved work with no model in the loop (OMN-17427).

The tick reads the headroom slots it is given and the approved-work list, drops owned
rows, orders the rest by priority class, launches through the remote-lane runner and
answers one result per tick naming what it dispatched and why the rest did not go.
"""

from __future__ import annotations

import importlib
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_fill_effect.handlers import (
    HandlerLabFillLaunch,
    HandlerLabFillTick,
)
from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_tick import (
    lane_name,
    priority_class,
)
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillTickHost,
    ModelLabFillTickRequest,
    ModelLabFillTickResult,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import LabFillClaimFacts

from .fakes import (
    FakeApproved,
    FakeBlocks,
    FakeChecks,
    FakeClock,
    FakeOwners,
    FakeRunner,
)


def _row(n: int, kind: str = "wiring", repo: str = "omnimarket") -> dict[str, object]:
    return {
        "id": f"row-{n}",
        "kind": kind,
        "ticket": f"OMN-{n}",
        "repo": repo,
        "goal": f"goal {n}",
        "acceptance_check": f"check {n}",
    }


def _owners(index: dict[str, dict[str, str]] | None = None) -> FakeOwners:
    return FakeOwners(
        claims_facts=LabFillClaimFacts(
            index=index or {}, open_claims=[], staleness_hours=12
        )
    )


def _request(tmp_path: Path, **kw: Any) -> ModelLabFillTickRequest:
    base: dict[str, Any] = {
        "run_key": "2026-10-17T0930Z",
        "fire_id": "20261017T093000Z-lab-fill",
        "hosts": (ModelLabFillTickHost(host="h201", free=2),),
        "ledger_path": "/l",
        "approved_work_path": "/a",
        "brief_dir": str(tmp_path / "briefs"),
        "operator_id": "op",
        "parent": "orchestrator-bb1a",
        "parent_ticket": "OMN-17427",
        "authority_ruling": "2026-10-17T09:03:06Z",
        "authority_lane": "orchestrator-bb1a",
        "process_repos": ("process_repo",),
        "mvp_repos": ("omnimarket",),
    }
    base.update(kw)
    return ModelLabFillTickRequest(**base)


def _tick(
    rows: list[dict[str, object]],
    *,
    owners: FakeOwners | None = None,
    checks: FakeChecks | None = None,
    runner: FakeRunner | None = None,
) -> tuple[HandlerLabFillTick, FakeRunner, FakeChecks]:
    runner = runner or FakeRunner()
    checks = checks or FakeChecks()
    approved = FakeApproved(rows={str(r["id"]): r for r in rows}, all_rows=rows)
    clock = FakeClock()
    launch = HandlerLabFillLaunch(
        checks=checks,
        approved=approved,
        blocks=FakeBlocks(),
        runner=runner,
        clock=clock,
    )
    handler = HandlerLabFillTick(
        approved=approved, owners=owners or _owners(), launch=launch, clock=clock
    )
    return handler, runner, checks


def test_contract_binds_the_tick_operation() -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_lab_fill_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    entry = next(
        e
        for e in contract["handler_routing"]["handlers"]
        if e["operation"] == "run_lab_fill_tick"
    )
    module = importlib.import_module(entry["handler"]["module"])
    assert getattr(module, entry["handler"]["name"]) is HandlerLabFillTick
    assert entry["input_model"].endswith(".ModelLabFillTickRequest")
    assert entry["output_model"].endswith(".ModelLabFillTickResult")
    published = {e["event_type"]: e["topic"] for e in contract["published_events"]}
    assert (
        published["ModelLabFillTickResult"]
        == contract["runtime_dispatch"]["terminal_events"]["success"]
    )


def test_tick_dispatches_free_slots_in_priority_order(tmp_path: Path) -> None:
    rows = [
        _row(1, kind="wiring", repo="omniclaude"),
        _row(2, kind="process-fix", repo="omnimarket"),
        _row(3, kind="wiring", repo="process_repo"),
    ]
    handler, runner, checks = _tick(rows)
    result = handler.handle(_request(tmp_path))
    assert isinstance(result, ModelLabFillTickResult)
    assert result.outcome == "dispatched"
    assert result.free_slots == 2
    # process-fix kind and a process repository outrank the other row; list order breaks ties
    assert [d.ticket for d in result.dispatched] == ["OMN-2", "OMN-3"]
    assert [d.host for d in result.dispatched] == ["h201", "h201"]
    assert all(d.receipt == "/state/x/receipt.json" for d in result.dispatched)
    assert result.untried == ("OMN-1",)
    assert result.skipped == ()
    # the Linear assignee/state re-check ran inside the launch, once per tried ticket
    assert [c["ticket"] for c in checks.calls] == ["OMN-2", "OMN-3"]
    args = runner.calls[0][0]
    assert args[args.index("--lane") + 1] == "lab-fill-omn2-10170930"
    assert args[args.index("--host") + 1] == "h201"
    assert args[args.index("--parent") + 1] == "orchestrator-bb1a"
    assert args[-1] == "--detach"
    brief = (tmp_path / "briefs" / "lab-fill-omn2-10170930.md").read_text()
    assert "GOAL: goal 2" in brief
    assert "ACCEPTANCE CHECK: check 2" in brief
    assert "AUTHORITY: operator RULING 2026-10-17T09:03:06Z" in brief
    assert "APPROVED-ROW" not in brief


def test_a_refused_candidate_frees_its_slot_for_the_next(tmp_path: Path) -> None:
    rows = [_row(1), _row(2), _row(3)]

    class Checks(FakeChecks):
        def skip_reason(self, **kw: str) -> tuple[str, str]:
            super().skip_reason(**kw)
            if kw["ticket"] == "OMN-1":
                return "assigned-other", "assignee someone"
            return "", ""

    handler, _, _ = _tick(rows, checks=Checks())
    result = handler.handle(_request(tmp_path))
    assert [d.ticket for d in result.dispatched] == ["OMN-2", "OMN-3"]
    assert [(s.ticket, s.reason) for s in result.skipped] == [
        ("OMN-1", "assigned-other")
    ]


def test_owned_and_blocked_rows_are_skipped_before_launch(tmp_path: Path) -> None:
    blocked = {**_row(2), "blocked_until": "OMN-9 Done"}
    rows = [_row(1), blocked, _row(3)]
    owners = _owners({"OMN-1": {"lane": "peer", "state": "held"}})
    handler, runner, checks = _tick(rows, owners=owners)
    result = handler.handle(_request(tmp_path))
    assert [d.ticket for d in result.dispatched] == ["OMN-3"]
    assert {(s.ticket, s.reason) for s in result.skipped} == {
        ("OMN-1", "owned:peer"),
        ("OMN-2", "blocked"),
    }
    assert [c["ticket"] for c in checks.calls] == ["OMN-3"]
    assert len(runner.calls) == 1


def test_plan_only_tick_launches_nothing(tmp_path: Path) -> None:
    handler, runner, checks = _tick([_row(1), _row(2)])
    result = handler.handle(_request(tmp_path, apply=False))
    assert result.outcome == "planned"
    assert [(d.ticket, d.host, d.receipt) for d in result.dispatched] == [
        ("OMN-1", "h201", ""),
        ("OMN-2", "h201", ""),
    ]
    assert runner.calls == []
    assert checks.calls == []


def test_slots_respect_per_host_and_run_caps(tmp_path: Path) -> None:
    rows = [_row(n) for n in range(1, 8)]
    handler, _, _ = _tick(rows)
    hosts = (
        ModelLabFillTickHost(host="h201", free=5),
        ModelLabFillTickHost(host="h202", free=3),
    )
    result = handler.handle(
        _request(tmp_path, hosts=hosts, max_per_host=2, max_lanes=3)
    )
    assert result.free_slots == 3
    assert [d.host for d in result.dispatched] == ["h201", "h201", "h202"]


def test_rows_without_a_kind_ticket_or_id_are_skipped_not_launched(
    tmp_path: Path,
) -> None:
    no_kind = {k: v for k, v in _row(1).items() if k != "kind"}
    bad_ticket = {**_row(2), "ticket": "not-a-ticket"}
    bad_id = {**_row(3), "id": "row 3/../x"}
    handler, runner, checks = _tick([no_kind, bad_ticket, bad_id, _row(4)])
    result = handler.handle(_request(tmp_path))
    assert [d.ticket for d in result.dispatched] == ["OMN-4"]
    assert [(s.row_id, s.reason) for s in result.skipped] == [
        ("row-1", "invalid-row"),
        ("row-2", "invalid-row"),
        ("row 3/../x", "invalid-row"),
    ]
    assert [c["ticket"] for c in checks.calls] == ["OMN-4"]
    assert len(runner.calls) == 1


def test_rows_of_one_priority_class_keep_list_order(tmp_path: Path) -> None:
    rows = [_row(n, repo="omniclaude") for n in (7, 3, 9, 1)]
    handler, _, _ = _tick(rows)
    result = handler.handle(
        _request(
            tmp_path, apply=False, hosts=(ModelLabFillTickHost(host="h201", free=4),)
        )
    )
    assert [d.ticket for d in result.dispatched] == ["OMN-7", "OMN-3", "OMN-9", "OMN-1"]


def test_partial_headroom_across_hosts_fills_only_the_free_slots(
    tmp_path: Path,
) -> None:
    handler, _, _ = _tick([_row(n) for n in range(1, 5)])
    hosts = (
        ModelLabFillTickHost(host="h201", free=0),
        ModelLabFillTickHost(host="h202", free=1),
        ModelLabFillTickHost(host="h203", free=2),
    )
    result = handler.handle(_request(tmp_path, hosts=hosts))
    assert result.free_slots == 3
    assert [(d.ticket, d.host) for d in result.dispatched] == [
        ("OMN-1", "h202"),
        ("OMN-2", "h203"),
        ("OMN-3", "h203"),
    ]
    assert result.untried == ("OMN-4",)


@pytest.mark.parametrize("field", ["ledger_path", "approved_work_path", "brief_dir"])
@pytest.mark.parametrize("bad", ["../etc", "/a/../../etc", "a\x00b", ""])
def test_request_refuses_paths_that_climb_out_or_are_empty(
    tmp_path: Path, field: str, bad: str
) -> None:
    with pytest.raises(ValidationError):
        _request(tmp_path, **{field: bad})


def test_lane_name_and_priority_class() -> None:
    assert lane_name("OMN-123", "2026-10-17T0930Z") == "lab-fill-omn123-10170930"
    assert lane_name("OMN-5", "2026-10-17") == "lab-fill-omn5-1017"
    assert priority_class({"kind": "process-fix", "repo": "x"}, (), ()) == "process-fix"
    assert (
        priority_class(
            {"kind": "wiring", "repo": "process_repo"}, (), ("process_repo",)
        )
        == "process-fix"
    )
    assert (
        priority_class(
            {"kind": "wiring", "repo": "omnimarket", "pr": "omnimarket#4"},
            ("omnimarket",),
            (),
        )
        == "mvp-landing"
    )
    assert (
        priority_class({"kind": "wiring", "repo": "omnimarket"}, ("omnimarket",), ())
        == "other"
    )


def test_local_approved_work_lists_every_row(tmp_path: Path) -> None:
    import json

    from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
        LocalApprovedWork,
    )

    path = tmp_path / "approved.json"
    path.write_text(json.dumps({"version": 1, "rows": [_row(1), "junk", _row(2)]}))
    assert [r["id"] for r in LocalApprovedWork().list_rows(str(path))] == [
        "row-1",
        "row-2",
    ]
