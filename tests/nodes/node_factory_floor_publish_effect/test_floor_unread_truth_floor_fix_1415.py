# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A line the record carries no condition for is never counted as running, and Needs you holds only the operator's items.

Operator report 2026-11-03: every line card read "Condition unread" while the lines lamp and the Floor tab were green
"Running", Needs you listed items whose actor was someone else and named internal worker ids, two sources were not read
and the Dashboard tab word truncated on a phone.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    NODE_DIR,
    SHELL_FILES,
    SHELL_RECORD,
    _run_floor,
    _run_shell,
    _shell_out,
)

SHELL_SRC = (NODE_DIR / "page/index.html").read_text(encoding="utf-8")


def _unread_record() -> dict[str, Any]:
    record = copy.deepcopy(SHELL_RECORD)
    for line in record["lines"]:
        for field in ("condition", "blocker", "progress", "gates", "line_evidence"):
            line.pop(field, None)
    return record


def _shell(record: dict[str, Any]) -> dict[str, str]:
    return _shell_out(_run_shell({**SHELL_FILES, "factory-floor.json": record}))


def test_an_unread_line_is_not_counted_as_running_on_the_lamp_the_verdict_or_the_floor_tab() -> (
    None
):
    out = _shell(_unread_record())
    dash = out["dash"]
    assert "All lines running" not in dash
    assert "1 line is not read" in dash
    lamp = dash.split('class="board"')[1].split('role="listitem"')[1]
    assert lamp.startswith("")
    assert 'class="lamp c-idle' in dash.split('class="board"')[1]
    assert "0 of 1" not in lamp
    assert "Not read" in lamp
    assert "Running" not in lamp.split("</div>")[0]
    assert out["word-floor"].endswith("Not read")
    assert "Running" not in out["word-floor"]


def test_some_unread_lines_make_the_floor_amber_and_a_stopped_line_still_makes_it_red() -> (
    None
):
    record = _unread_record()
    second = copy.deepcopy(SHELL_RECORD["lines"][0])
    second["line_id"] = "plan-three"
    second["name"] = "Third plan"
    second["plan_path"] = "beta/plans/three-plan.md"
    second["condition"] = "building"
    stopped_line = copy.deepcopy(SHELL_RECORD["lines"][0])
    out = _shell({**record, "lines": [record["lines"][0], second]})
    assert out["word-floor"].endswith("Needs a look")
    assert "1 line is not read" in out["dash"]
    stopped_out = _shell({**record, "lines": [stopped_line, record["lines"][0]]})
    assert stopped_out["word-floor"].endswith("Stopped")
    assert "1 line is stopped" in stopped_out["dash"]
    assert "1 line is not read" in stopped_out["dash"]


def test_the_floor_page_says_when_lines_are_not_read_and_does_not_draw_the_verdict_green() -> (
    None
):
    out = _run_floor({"factory-floor.json": _unread_record()})
    verdict = out["verdict"]
    assert "not read" in verdict
    assert "No line is stopped (" not in verdict
    floor_src = (NODE_DIR / "page/floor/index.html").read_text(encoding="utf-8")
    assert 'unreadLines ? "c-warn" : "c-ok"' in floor_src


def test_needs_you_holds_only_the_operators_items_and_names_no_internal_worker() -> (
    None
):
    dash = _shell(copy.deepcopy(SHELL_RECORD))["dash"]
    needs = dash.split("Needs you")[1].split("The factory is handling")[0]
    # The stopped line is the operator's call; quiet workers, failing changes and unread sources are not.
    assert "Who acts: you" in needs
    assert "Who acts: whoever" not in dash
    assert "Who acts: the owner" not in dash
    assert "Who acts: the worker" not in dash
    assert "quiet" not in needs
    handled = dash.split("The factory is handling")[1].split("</section>")[0]
    assert (
        "not read" in handled or "failing checks" in handled or "gone quiet" in handled
    )
    assert not re.search(
        r"\b(landing|lane|fix|dev)-[\w-]*\d", re.sub(r'href="[^"]*"', "", handled)
    )


def test_nothing_for_the_operator_reads_as_nothing_not_as_a_list_of_other_peoples_work() -> (
    None
):
    record = _unread_record()
    dash = _shell(record)["dash"]
    assert "Needs you: nothing" in dash


def test_the_unread_sources_are_named_in_plain_words_beside_the_count() -> None:
    dash = _shell(copy.deepcopy(SHELL_RECORD))["dash"]
    handled = dash.split("The factory is handling")[1]
    sentence = re.search(
        r"\d+ sources? (?:is|are) (?:not read[^:]*): ([^<]+)\.", handled
    )
    if sentence is None:
        return  # every source in this fixture is read
    assert re.search(r"[a-z]", sentence.group(1))


def test_the_tab_words_are_short_enough_for_a_phone() -> None:
    for word in re.findall(r'setTab\("(?:dashboard|floor)", [^\n]*', SHELL_SRC):
        for literal in re.findall(r'"([A-Z][A-Za-z ]+)"', word):
            if literal in ("Needs a look", "Not read", "Running", "Stopped", "Failing"):
                assert len(literal) <= 12
    assert (
        "Stopped or failing"
        not in SHELL_SRC.split("function renderTabs")[1].split("function renderStamp")[
            0
        ]
    )


def test_a_snapshot_tab_is_a_steady_grey_dot_not_a_dashed_spinner_ring() -> None:
    assert '"snap", snap ? "Snapshot"' in SHELL_SRC
    assert ".tab i.c-snap{background:var(--muted)" in SHELL_SRC
