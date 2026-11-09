# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: no fill brief that opens a pull request is refused by lane-brief's child-id check (OMN-22866).

From 2026-11-08 21:57Z every lab-fill fire read ``dispatched=0 ... brief-check-refused`` for every eligible
ticket (1, then up to 7 of them, with 41 free slots on 2026-11-09 04:10Z). The cause: lane-brief's
``check --lab`` (OMN-22883, merged 2026-11-08 21:25Z) refuses a brief whose own prose opens a pull request and
carries neither a ``child-ticket-ids:`` line nor a push-the-branch-and-report sentence, and the fill briefs
said "open one PR whose title contains <ticket>" and nothing about pushing.

The check lives in the lane-brief skill's guard node and omnimarket cannot import it, so ``child_id_problems`` below is a
copy of its three patterns (``OPENS_PR_RE``, ``PUSH_REPORT_RE``, ``MINT_NEGATION_RE`` in
``node_release_plan_guard_compute/handler.py`` of the plugin repository); the named injected break is the brief with the pushed-branch
line cut out, which the copy must read as refused.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.models.lab_fill import PUSH_AND_REPORT_LINE
from omnimarket.nodes.node_lab_fill_effect.handlers import handler_lab_fill_tick
from omnimarket.nodes.node_lab_fill_effect.models import ModelLabFillTickAlarm
from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_dispatch_plan import (
    DISPATCH_KINDS,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_lane_render import (
    HandlerLabFillLaneRender,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillDispatchItem,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models.model_lab_fill_lane_render import (
    ModelLabFillLaneRenderRequest,
    ModelLabFillRenderConfig,
)

from .fakes import FakeClock
from .test_golden_chain_lab_fill_zero_placement import (
    Memory,
    ScriptedRunner,
    _handler,
    _request,
    _row,
)

pytestmark = pytest.mark.unit

_OPEN_VERBS = r"(?:open(?:s|ing)?|creat(?:e|es|ing)|rais(?:e|es|ing)|submit(?:s|ting)?)"
_PR_NOUN = r"\b(?:pull[ \t]+requests?|PRs?)\b"
OPENS_PR_RE = re.compile(
    rf"^[ \t]*(?:[-*][ \t]+)?{_OPEN_VERBS}\b(?!-)[^.;]{{0,60}}?{_PR_NOUN}"
    rf"|\b{_OPEN_VERBS}\b(?!-)[ \t]+[^.;\s]+[ \t]+[^.;]{{0,60}}?{_PR_NOUN}"
    r"|\bone[ \t]+(?:pull[ \t]+request|PR)[ \t]+each\b",
    re.IGNORECASE,
)
PUSH_REPORT_RE = re.compile(
    r"\bpush(?:es)?\b[^.;]{0,60}?\bbranch\b[^.]{0,200}?\b(?:report|MSG|TERMINAL|name[sd]?)\b",
    re.IGNORECASE,
)
MINT_NEGATION_RE = re.compile(
    r"\b(?:never|not|nothing|without|refus\w*|forbid\w*|avoid\w*|out of scope|no[ \t]+(?:mint|creat)\w*)\b|n't",
    re.IGNORECASE,
)
CHILD_IDS_BLOCK_RE = re.compile(r"^[ \t]*child-ticket-ids:[ \t]*OMN-\d+", re.MULTILINE)


def child_id_problems(brief: str) -> list[str]:
    """The check's verdict on the brief's own prose: the opening sentences it names, or none when it admits the brief."""
    sentences = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+|\n", brief)
        if s.strip() and not MINT_NEGATION_RE.search(s)
    ]
    hits = [h.group(0) for s in sentences if (h := OPENS_PR_RE.search(s))]
    if (
        not hits
        or CHILD_IDS_BLOCK_RE.search(brief)
        or any(PUSH_REPORT_RE.search(s) for s in sentences)
    ):
        return []
    return hits


def _rendered(kind: str) -> str:
    item = ModelLabFillDispatchItem(
        lane=f"lab-fill-{kind}",
        kind=kind,
        id="row-1",
        ticket="OMN-9",
        pr="",
        repo="repo_a",
        ref="main",
        title="the ticket title",
        updated_at="2026-11-07T00:00:00Z",
        host="h1",
        pinned=False,
        engine="sonnet",
        route="ROUTE: band=B3 score=10",
    )
    config = ModelLabFillRenderConfig(
        pillar="landing",
        parent_lane="lab-fill",
        parent_ticket="OMN-22866",
        run_key="2026-11-09",
        authority_ruling="2026-10-10T04:08:23Z",
        authority_lane="ruling-idle-lab-9f8a",
    )
    return (
        HandlerLabFillLaneRender()
        .handle(ModelLabFillLaneRenderRequest(item=item, config=config))
        .brief
    )


def test_the_push_sentence_is_an_instruction_to_the_check() -> None:
    assert PUSH_REPORT_RE.search(PUSH_AND_REPORT_LINE)
    assert not MINT_NEGATION_RE.search(PUSH_AND_REPORT_LINE)


@pytest.mark.parametrize("kind", sorted(DISPATCH_KINDS))
def test_every_rendered_dispatch_kind_passes_the_child_id_check(kind: str) -> None:
    assert child_id_problems(_rendered(kind)) == []


def test_failing_control_the_ticket_brief_without_the_line_is_refused() -> None:
    """The injected break: cut the pushed-branch line out and the copied check names 'open one PR'."""
    broken = _rendered("ticket").replace(PUSH_AND_REPORT_LINE, "")
    assert PUSH_AND_REPORT_LINE not in broken
    problems = child_id_problems(broken)
    assert problems, (
        "the ticket brief opens a pull request, so without the line the check must refuse it"
    )
    assert any("open one PR" in p for p in problems)


def test_a_child_ticket_ids_line_would_also_admit_the_broken_brief() -> None:
    """Positive control for the copy: the check's other way out reads as admitted."""
    broken = _rendered("ticket").replace(PUSH_AND_REPORT_LINE, "")
    assert child_id_problems(broken + "\nchild-ticket-ids: OMN-1\n") == []


def test_the_tick_brief_and_the_diagnose_brief_pass_the_child_id_check(
    tmp_path: Path,
) -> None:
    runner = ScriptedRunner()
    clock = FakeClock()
    result = _handler([_row(22294)], runner, Memory(), clock).handle(_request(tmp_path))
    brief_file = Path(_request(tmp_path).brief_dir) / f"{result.dispatched[0].lane}.md"
    written = brief_file.read_text(encoding="utf-8")
    assert "Opens PR in: omnimarket" in written
    assert child_id_problems(written) == []

    request = _request(tmp_path)
    diagnose = handler_lab_fill_tick._diagnose_brief(
        request,
        "lab-fill-diagnose-x",
        result,
        ModelLabFillTickAlarm(streak=2, free_slots=3),
    )
    assert "Opens PR in: none" in diagnose
    assert child_id_problems(diagnose) == []
    assert child_id_problems(diagnose.replace(PUSH_AND_REPORT_LINE, "")) != [], (
        "'Opens PR in: none' reads as an opening sentence to the check, so the diagnose lane needs the line too"
    )
