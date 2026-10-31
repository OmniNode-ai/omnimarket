# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plain-language dashboard (design review F01 F02 F04 F08 F10 F17 and the dashboard part of F03 F05 F06 F09 F14 F15)."""

from __future__ import annotations

import re

from .test_factory_floor_publish import (
    NODE_DIR,
    SHELL_FILES,
    _run_shell,
    _shell_out,
)

SHELL = (NODE_DIR / "page/index.html").read_text(encoding="utf-8")
CSS = SHELL.split("</style>")[0]


def _dash() -> str:
    return _shell_out(_run_shell(SHELL_FILES))["dash"]


def test_f01_the_dashboard_opens_with_one_verdict_sentence_and_a_needs_you_list() -> (
    None
):
    dash = _dash()
    assert dash.index('class="verdict') < dash.index('class="board"')
    assert "Needs attention: 1 line is stopped" in dash
    assert "Needs you" in dash
    needs = dash.split('class="needs"')[1].split("</ol>")[0]
    assert 1 <= needs.count("<li>") <= 5
    assert 'href="#floor/line-plan-two"' in dash
    assert "All lines running. Nothing needs you." in SHELL


def test_f02_one_lamp_one_measure_with_a_written_colour_rule() -> None:
    dash = _dash()
    assert "changes failing checks" in dash
    assert "green: running as expected" in dash
    assert "amber: needs a look" in dash
    assert "red: stopped or failing" in dash
    assert "grey dashed: no data" in dash
    assert "sources not read" in dash or "source not read" in dash


def test_f04_stopped_lamps_are_steady_with_a_word_and_pulse_once() -> None:
    assert "blink" not in SHELL
    assert "infinite" not in SHELL
    assert re.search(r"pulse 1s ease-out 1\b", SHELL)
    assert ">Stopped<" in _dash()


def test_f08_machines_are_one_row_each_and_freshness_is_one_line() -> None:
    dash = _dash()
    assert 'class="mrow' in dash
    assert 'class="bay ' not in dash
    assert re.search(r"\d+ of \d+ sources fresh", dash)
    assert "Show all" in dash


def test_f09_unmatched_lines_collapse_into_one_sentence() -> None:
    dash = _dash()
    assert "more line is planned but has no plan file yet (show)" in dash
    assert '<article class="card ghost"' not in dash


def test_f10_the_header_age_follows_the_selected_tab() -> None:
    assert "tabAge[currentTab]" in SHELL
    assert "Snapshot from" in SHELL
    assert (
        "renderStamp();" in SHELL.split("function show(")[1].split("function flash")[0]
    )


def test_f17_the_document_scrolls_once_and_the_header_is_sticky() -> None:
    body_rule = re.search(r"\nbody\{[^}]*\}", CSS)
    assert body_rule
    assert "overflow" not in body_rule.group(0)
    assert "position:sticky;top:0" in CSS
    assert ".pane{position:absolute" not in CSS
    assert "sizeToContent" in SHELL


def test_f03_the_glossary_words_replace_the_internal_ones_on_the_dashboard() -> None:
    dash = _dash()
    for old in ("andon", "CI red", "ledger row", "pull request"):
        assert old not in dash, old
    assert "worker" in dash
    assert "failing checks" in dash


def test_f05_times_are_eastern_with_a_relative_age_and_utc_only_in_a_title() -> None:
    assert 'const ZONE = "America/New_York"' in SHELL
    assert " ET" in SHELL
    assert SHELL.count('" UTC"') == 1  # only the hover-title helper
    assert 'title="${esc(utcText(' in SHELL


def test_f06_every_strip_has_counts_and_a_legend() -> None:
    dash = _dash()
    assert re.search(r"\d+ running", dash)
    assert "What the slot colours mean" in dash
    assert "What the part colours mean" in dash
    assert 'id="word-${t.id}"' in SHELL


def test_f14_f15_type_floor_and_darker_status_tokens() -> None:
    sizes = [float(x) for x in re.findall(r"font(?:-size)?:[^;{}]*?(\d*\.\d+)rem", CSS)]
    assert sizes
    assert min(sizes) >= 0.75
    assert not re.search(r"font:[^;}]*var\(--f-mono\)", CSS.split(".mono{")[1])
    assert "--ok-t:#1f6b38" in CSS
    assert "--warn-t:#8a5300" in CSS
    assert "--stop-t:#a3271f" in CSS
    assert ".pill.stop{color:var(--stop-t)}" in CSS
