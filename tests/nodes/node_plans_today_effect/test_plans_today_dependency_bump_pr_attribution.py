# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A pull request is the work of a task only when its own title or branch names the task's ticket.

The recorded case: omnimemory#713 is a Dependabot bump of workflow pins. Its body is a list of the commit titles of
the repositories it bumps, and two of those titles name OMN-21495 and OMN-21515. Reading ticket ids out of the body
attributed the bump to the validators plan's tasks T0.2 and T1.1, so the line read Stopped because of an unrelated
bump's red check.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_plans_today_effect.handlers import (
    handler_plans_today as plans_today,
)
from tests.nodes.node_plans_today_effect.conftest import (
    body,
    by_title,
    invoke,
    make_fixture,
    pr,
)

pytestmark = pytest.mark.unit

# The watcher entry of omnimemory#713 as recorded at 2026-11-04T02:41:48Z: the title, the branch, the author and the
# lines of the body that name the plan's tickets (the body there is 20000 characters of copied commit titles).
RECORDED_BUMP_BODY = "\n".join(
    [
        "Bumps the actions group with 6 updates:",
        "<details>",
        "<summary>Commits</summary>",
        "<ul>",
        "<li>T0.2: pin parity for the shared core hook repo (OMN-21495) by <a>@example-author</a> in <a>OmniNode-ai/omnibase_core#2111</a></li>",
        "<li>T1.1+K2: canonical file shape node, lifecycle rule, handler_outside_node and cli_entry_outside_node (OMN-21515) by <a>@example-author</a> in <a>OmniNode-ai/omnibase_core#2124</a></li>",
        "<li>fix(OMN-19998): internal-sibling-pins accepts a published PyPI release behind...</li>",
        "</ul>",
        "</details>",
    ]
)


def recorded_bump() -> dict[str, Any]:
    entry = pr("OMN-0", cls="red")
    entry["facts"].update(
        {
            "title": "build(deps): bump the actions group with 6 updates",
            "body": RECORDED_BUMP_BODY,
            "head_ref": "dependabot/github_actions/actions-e4afbbc559",
            "author": "dependabot[bot]",
        }
    )
    return entry


def own_pr(ticket: str) -> dict[str, Any]:
    entry = pr(ticket, cls="green")
    entry["facts"]["head_ref"] = f"omn-{ticket.removeprefix('OMN-')}-the-work"
    return entry


PLANS = {
    "2026-09-28-validators.md": body(
        "Validators",
        [("pin parity", "OMN-21495"), ("file shape node", "OMN-21515")],
    )
}


def test_load_watcher_does_not_attribute_a_bump_by_the_ids_in_its_body(tmp_path):
    fx = make_fixture(
        tmp_path, pytest.MonkeyPatch(), PLANS, {"omnimemory#713": recorded_bump()}
    )
    watcher = plans_today.load_watcher(
        fx["watcher"],
        plans_today.parse_ts("2026-09-29T16:00:00Z"),
        max_age_min=60,
    )
    assert "omnimemory#713" in watcher.prs
    assert watcher.by_ticket == {}


def test_a_pr_is_attributed_by_its_title_and_by_its_branch(tmp_path):
    prs = {
        "omnibase_core#1": pr("OMN-21495", cls="green"),
        "omnibase_core#2": own_pr("OMN-21515"),
        "omnimemory#713": recorded_bump(),
    }
    fx = make_fixture(tmp_path, pytest.MonkeyPatch(), PLANS, prs)
    watcher = plans_today.load_watcher(
        fx["watcher"],
        plans_today.parse_ts("2026-09-29T16:00:00Z"),
        max_age_min=60,
    )
    assert watcher.by_ticket == {
        "OMN-21495": ["omnibase_core#1"],
        "OMN-21515": ["omnibase_core#2"],
    }


def test_the_validators_tasks_do_not_read_open_red_from_the_bump(tmp_path, monkeypatch):
    fx = make_fixture(tmp_path, monkeypatch, PLANS, {"omnimemory#713": recorded_bump()})
    code, doc = invoke(fx)
    assert code == 0
    plan = by_title(doc)["Validators"]
    for task in plan["tasks"]:
        assert task["prs"] == [], task
        assert task["state"] == "not-started", task
        assert "watcher-open-pr" not in task["state_basis"], task


def test_a_task_still_reads_open_red_from_its_own_red_pr(tmp_path, monkeypatch):
    prs = {
        "omnibase_core#1": pr("OMN-21495", cls="red"),
        "omnimemory#713": recorded_bump(),
    }
    fx = make_fixture(tmp_path, monkeypatch, PLANS, prs)
    code, doc = invoke(fx)
    assert code == 0
    tasks = {t["label"]: t for t in by_title(doc)["Validators"]["tasks"]}
    own = next(t for t in tasks.values() if "OMN-21495" in t["tickets"])
    other = next(t for t in tasks.values() if "OMN-21515" in t["tickets"])
    assert own["prs"] == ["omnibase_core#1"]
    assert own["state"] == "open-red"
    assert other["prs"] == []
    assert other["state"] == "not-started"
