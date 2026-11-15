# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1: one brief per omnimarket producer passes the real gate under ``enforce``.

Every repository name and id below is an invented fixture. The producers are the code paths that
write a brief naming a repository: the dev red-cause receiver, the controller fix plan, the exception
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
    "dev_cause_receiver": _dev_cause,
    "controller_fix_plan": _controller_fix,
    "exception_queue": _exception_queue,
    "brief_assembler": _assembled,
}


@pytest.mark.parametrize("name", sorted(PRODUCERS))
def test_ac1_each_producers_brief_passes_the_real_gate_under_enforce(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = PRODUCERS[name](tmp_path)
    repo = "omnimarket" if name == "brief_assembler" else REPO_A
    assert f"\nOpens PR in: {repo}\n" in text
    result = check(
        text.replace(f"Opens PR in: {repo}", f"Opens PR in: {REPO_A}"),
        config=ENFORCE,
        state=ROOM,
    )
    assert result.verdict is Verdict.ADMITTED, result.line


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
