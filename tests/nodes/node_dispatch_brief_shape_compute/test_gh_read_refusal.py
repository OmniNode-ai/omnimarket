# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dispatch brief-shape refusal ``gh-read-refused`` (OMN-18946): a brief that tells a lane to run a gh call is
refused before placement, with the mirror named; the rules block and the 3a block name gh only to forbid it."""

from __future__ import annotations

from types import ModuleType

import pytest

from omnimarket.nodes.node_dispatch_brief_shape_compute import (
    NodeDispatchBriefShapeCompute,
)
from omnimarket.nodes.node_dispatch_brief_shape_compute.handlers import (
    handler_dispatch_brief_shape as shape_handler,
)
from omnimarket.nodes.node_dispatch_brief_shape_compute.models.model_dispatch_brief_shape import (
    ModelDispatchBriefShapeRequest,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def readers(monkeypatch):
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    module = ModuleType("fixture_brief_readers_gh")
    module.RULES_BLOCK_HEAD = "# Standing rules"
    module.H_CLAIM = "claim"
    module.H_3A = "## 3a. Constraints"
    module.unfenced_prose = lambda text: text
    module.without_ticket_text = lambda text: text
    module.premise_text = lambda text: text.split(module.RULES_BLOCK_HEAD)[0]
    module.split_parts = lambda _text: ({}, ())
    module.has_contract_step = lambda _text, _ticket: False
    module.lab_mint_problems = lambda _text: []
    monkeypatch.setattr(shape_handler, "load_lane_brief", lambda _path: module)
    return module


def refusals(brief: str) -> tuple[str, ...]:
    result = NodeDispatchBriefShapeCompute().handle(
        ModelDispatchBriefShapeRequest(brief, "lane-a", (), "unused.py")
    )
    return tuple(status for status, _ in result.refusals)


def test_a_brief_that_tells_the_lane_to_run_gh_run_view_is_refused_before_placement(
    readers,
) -> None:
    brief = "Find the cause.\nWhen the job-log cache misses, run `gh run view --log` on the failing run.\n"
    result = NodeDispatchBriefShapeCompute().handle(
        ModelDispatchBriefShapeRequest(brief, "cause-a", (), "unused.py")
    )
    assert [s for s, _ in result.refusals] == ["gh-read-refused"]
    line = result.refusals[0][1]
    assert line.startswith("REMOTE-LANE-GH-READ-REFUSED ")
    assert "onex scm run-view" in line
    assert "line 2" in line
    # Positive control: the same brief naming the mirror is admitted.
    assert (
        refusals(
            "Find the cause.\nOn a cache miss say so; read onex scm job-log o/r --job-id 7.\n"
        )
        == ()
    )


def test_the_rules_block_and_the_3a_block_name_gh_only_to_forbid_it(readers) -> None:
    brief = (
        "Fix the red.\n# Standing rules\nNever run gh pr view; gh pr checks is refused.\n"
        "gh run view --log is how it used to be done\n## 3a. Constraints\ngh api graphql is refused\n"
    )
    assert refusals(brief) == ()
    # Negative control: the same line in the brief's own words is refused.
    assert refusals("Fix the red.\ngh run view --log is how to read it\n") == (
        "gh-read-refused",
    )


def test_a_fenced_command_in_the_brief_is_refused(readers) -> None:
    brief = "Read the head:\n```bash\ngh pr view 9 --repo o/r --json headRefOid\n```\n"
    assert refusals(brief) == ("gh-read-refused",)
