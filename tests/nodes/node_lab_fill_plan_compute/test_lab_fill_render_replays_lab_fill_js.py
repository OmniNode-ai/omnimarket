# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane brief render replaces lab-fill.js's own laneTask, brief for brief (OMN-22866).

The running lab-fill workflow rendered its lane briefs with its own ``laneTask`` and ``fallbackTask``, so the sentence that
lane-brief's ``check --lab`` needs (the pushed branch named in the TERMINAL) that omnimarket#5548 added to this renderer never
reached a brief: from 2026-11-08 21:57Z every fire refused every eligible ticket (``brief-check-refused``, 7 of 7 on the 05:50Z,
06:10Z and 06:30Z fires of 2026-11-09). ``parity_cases_outcomes.json`` holds the briefs the script's pure block answered under node
for every fill kind it renders, pinned and unpinned, with both fallback shapes. They are the old behaviour: the replay control
below shows the check refusing the ones that open a pull request, and the render answering the same text plus the one sentence
(``test_lab_fill_outcome_parity`` compares it cell by cell).
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

from omnimarket.models.lab_fill import PUSH_AND_REPORT_LINE
from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_lane_render import (
    HandlerLabFillLaneRender,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models.model_lab_fill_lane_render import (
    ModelLabFillLaneRenderRequest,
)

from ..node_lab_fill_effect.test_error_chain_lab_fill_brief_check_omn22866 import (
    child_id_problems,
)
from .test_lab_fill_outcome_parity import CASES

pytestmark = pytest.mark.unit

_CASES: list[dict[str, Any]] = [
    case for case in CASES if case["op"] == "render_lab_fill_lane"
]
_IDS = [case["name"] for case in _CASES]
_OPENS = (" open one PR ", " open a PR ")
_OLD = {case["name"]: case["expected"]["brief"] for case in _CASES}


def _render(case: dict[str, Any]) -> str:
    return (
        HandlerLabFillLaneRender()
        .handle(ModelLabFillLaneRenderRequest.model_validate(case["input"]))
        .brief
    )


def test_the_recorded_cases_cover_every_kind_the_workflow_renders() -> None:
    kinds = {
        case["input"]["item"]["kind"]
        for case in _CASES
        if "from_lane" not in case["input"]["item"]
    }
    assert {
        "ticket",
        "defect",
        "pr-red",
        "pr-stalled",
        "plan-stalled",
        "lane-resend",
        "ruling-pending",
        "process-fix",
        "partial-node",
        "wiring",
    } <= kinds
    assert sum("from_lane" in case["input"]["item"] for case in _CASES) >= 4


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_every_rendered_brief_passes_the_child_id_check(case: dict[str, Any]) -> None:
    rendered = _render(case)
    assert rendered.count(PUSH_AND_REPORT_LINE) == 1
    assert child_id_problems(rendered) == []


def test_failing_control_the_recorded_old_briefs_that_open_a_pr_are_refused() -> None:
    """Replay of the old behaviour: the check refuses the briefs the running workflow wrote, for 'open one PR'."""
    refused = {
        name: child_id_problems(brief)
        for name, brief in _OLD.items()
        if any(opening in brief for opening in _OPENS)
    }
    assert len(refused) >= 14, sorted(refused)
    assert all(refused.values()), [name for name, hits in refused.items() if not hits]
    assert any("open one PR" in hit for hits in refused.values() for hit in hits)
    assert not any(PUSH_AND_REPORT_LINE in brief for brief in _OLD.values())
    # The landing kinds never open one, so the old text was admitted: the refusal is the opening sentence, not the check.
    assert child_id_problems(_OLD["render-pr-red-escalated"]) == []
    assert child_id_problems(_OLD["render-pr-stalled-escalated"]) == []


def test_the_cli_writes_the_handler_brief_for_the_workflow_dispatch() -> None:
    case = next(case for case in _CASES if case["name"] == "render-ticket-pin-False")
    done = subprocess.run(
        [sys.executable, "-m", "omnimarket.nodes.node_lab_fill_plan_compute"],
        input=json.dumps(case["input"]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == _render(case)
    assert child_id_problems(done.stdout) == []


def test_the_cli_refuses_a_request_that_does_not_validate() -> None:
    case = next(case for case in _CASES if case["name"] == "render-ticket-pin-False")
    request = {
        **case["input"],
        "config": {**case["input"]["config"], "authority_ruling": "yesterday"},
    }
    done = subprocess.run(
        [sys.executable, "-m", "omnimarket.nodes.node_lab_fill_plan_compute"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2
    assert "authority_ruling" in done.stderr
    assert done.stdout == ""
