# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1: one brief per omnimarket producer passes the real gate under ``enforce``.

Every repository name and id below is an invented fixture. The producers are the code paths that
write a brief naming a repository: the idle-slot fill, its cloud backlog, the lab fill (a ticket
row and its diagnose lane), the dev red-cause receiver, the controller fix plan, the exception
queue and the brief assembler.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnimarket.models.lane_brief_pr_gate.enum_lane_brief_pr_gate_verdict import (
    EnumLaneBriefPrGateVerdict as Verdict,
)
from omnimarket.nodes.node_controller_fix_plan_compute.handler import build_brief
from omnimarket.nodes.node_dev_cause_lane_receiver_effect.handlers import (
    handler_dev_cause_lane_start as dev_cause,
)
from omnimarket.nodes.node_exception_queue_effect import handler as exception_queue
from omnimarket.nodes.node_idle_slot_fill_effect import cloud_backlog
from omnimarket.nodes.node_idle_slot_fill_effect.handlers import handler_idle_slot_fill
from omnimarket.nodes.node_lab_fill_effect.handlers import handler_lab_fill_tick
from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import ENV
from tests.nodes.node_lane_brief_draft_compute.test_assemble_lane_brief import (
    _request as assemble_request,
)
from tests.nodes.node_lane_brief_pr_gate_compute.test_pr_gate import (
    ENFORCE,
    REPO_A,
    ROOM,
    check,
)

pytestmark = pytest.mark.unit

TICKET = "OMN-90001"


def _idle_fill(tmp_path: Path) -> str:
    item = {"kind": "ticket", "key": "k1", "repo": REPO_A, "fact": "f"}
    return handler_idle_slot_fill.brief(item, "lane-a", "sonnet", "parent-a", TICKET)


def _cloud_backlog(tmp_path: Path) -> str:
    item = {"ticket": TICKET, "repo": REPO_A, "source": "s", "fact": "f"}
    return cloud_backlog.brief(item, "cloud-lane", "parent-a")


def _lab_fill_row(tmp_path: Path) -> str:
    request = SimpleNamespace(
        engine="codex",
        authority_ruling="RULING x",
        authority_lane="lane-x",
        parent_ticket=TICKET,
        parent="parent-a",
        landing_lane="landing-controller",
    )
    row = {"ticket": TICKET, "kind": "approved", "id": "r1", "repo": REPO_A}
    return handler_lab_fill_tick._brief(request, "lane-a", row, "marker")


def _dev_cause(tmp_path: Path) -> str:
    return dev_cause._BRIEF.format(
        model="sonnet",
        lane="lane-a",
        cause="c1",
        repo=REPO_A,
        branch="dev",
        head_sha="abc123",
        check="unit",
        observed_at="2026-10-27T12:00:00Z",
    )


def _controller_fix(tmp_path: Path) -> str:
    contract = {
        "controller_fix_route": "route model=sonnet dest=sonnet-lane effort=high",
        "controller_fix_brief_prs": 1,
        "controller_fix_repo": REPO_A,
        "ticket": TICKET,
        "ruling": "RULING x",
    }
    defect = {
        "key": f"{REPO_A}#1",
        "head": "abc123",
        "age": 90.0,
        "merge_state": "CLEAN",
        "armed": False,
        "title": "t",
    }
    return build_brief([defect], {}, contract, "lane-a", 30.0)


def _exception_queue(tmp_path: Path) -> str:
    request = SimpleNamespace(
        row=SimpleNamespace(cls="class-a"),
        parent="parent-a",
        state_dir=tmp_path,
    )
    rows = [
        {
            "repo": REPO_A,
            "id": "i1",
            "key": "k1",
            "source": "s",
            "evidence": "e",
            "tried": "t",
            "proposed_fix": "p",
        }
    ]
    path = exception_queue._brief(request, "lane-a", rows, "sonnet")
    return path.read_text(encoding="utf-8")


def _assembled(tmp_path: Path) -> str:
    from omnimarket.nodes.node_lane_brief_draft_compute.handlers.handler_lane_brief_assemble import (
        HandlerLaneBriefAssemble,
    )

    return HandlerLaneBriefAssemble().handle(assemble_request()).brief


PRODUCERS: dict[str, Callable[[Path], str]] = {
    "idle_slot_fill": _idle_fill,
    "cloud_backlog": _cloud_backlog,
    "lab_fill": _lab_fill_row,
    "dev_cause_receiver": _dev_cause,
    "controller_fix_plan": _controller_fix,
    "exception_queue": _exception_queue,
    "brief_assembler": _assembled,
}


@pytest.mark.parametrize("name", sorted(PRODUCERS))
def test_ac1_each_producers_brief_passes_the_real_gate_under_enforce(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", ENV["ONEX_SKILL_OVERLAY_ROOTS"])
    text = PRODUCERS[name](tmp_path)
    repo = "omnimarket" if name == "brief_assembler" else REPO_A
    assert f"\nOpens PR in: {repo}\n" in text
    result = check(
        text.replace(f"Opens PR in: {repo}", f"Opens PR in: {REPO_A}"),
        config=ENFORCE,
        state=ROOM,
    )
    assert result.verdict is Verdict.ADMITTED, result.line


def test_ac1_the_lab_fill_diagnose_brief_declares_it_opens_no_pr() -> None:
    request = SimpleNamespace(
        engine="codex",
        authority_ruling="RULING x",
        authority_lane="lane-x",
        parent_ticket=TICKET,
        parent="parent-a",
        landing_lane="landing-controller",
        run_key="rk",
        fire_id="f1",
    )
    result = SimpleNamespace(
        skipped=[],
        refusals=[],
        free_slots=2,
        placed=0,
        outcome="o",
        refusals_cell="-",
        parked=[],
    )
    alarm = SimpleNamespace(streak=3)
    text = handler_lab_fill_tick._diagnose_brief(request, "lane-a", result, alarm)
    assert check(text, config=ENFORCE).verdict is Verdict.ADMITTED


def test_a_producer_with_no_repository_leaves_the_line_out_and_the_gate_reports_it() -> (
    None
):
    contract = {
        "controller_fix_route": "route model=sonnet dest=sonnet-lane effort=high",
        "controller_fix_brief_prs": 1,
        "ticket": TICKET,
        "ruling": "RULING x",
    }
    text = build_brief([], {}, contract, "lane-a", 30.0)
    assert "Opens PR in" not in text
    assert check(text, config=ENFORCE).verdict is Verdict.REFUSED
