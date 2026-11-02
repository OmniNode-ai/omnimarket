# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The merged-and-running panel of the Factory floor page (owned page: page/floor/index.html)."""

from __future__ import annotations

import re
from pathlib import Path

PAGE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/floor/index.html"
).read_text(encoding="utf-8")
_SCRIPT_MATCH = re.search(r"<script>(.*)</script>", PAGE, re.DOTALL | re.IGNORECASE)
assert _SCRIPT_MATCH is not None
SCRIPT = _SCRIPT_MATCH.group(1)
BODY = PAGE.split("</style>")[-1]
CSS = "\n".join(re.findall(r"<style>(.*?)</style>", PAGE, re.DOTALL))


def _function(name: str) -> str:
    start = SCRIPT.index(f"function {name}(")
    depth, i = 0, SCRIPT.index("{", start)
    first = i
    while True:
        depth += {"{": 1, "}": -1}.get(SCRIPT[i], 0)
        if depth == 0 and i > first:
            return SCRIPT[start : i + 1]
        i += 1


def test_merged_running_panel_is_in_the_page_and_hidden_until_the_record_carries_it() -> (
    None
):
    assert re.search(r'<section[^>]*id="mergedRunning"[^>]*hidden', PAGE)
    assert "renderMergedRunning(floor)" in _function("render")


def test_merged_running_record_without_the_field_draws_nothing() -> None:
    body = _function("renderMergedRunning")
    guard = body[: body.index("return")]
    assert "floor.merged_running" in guard
    assert "hidden = true" in body
    assert 'innerHTML = ""' in body


def test_merged_running_draws_merged_running_not_running_and_idle_slots_for_the_eastern_day() -> (
    None
):
    body = _function("renderMergedRunning")
    for word in ("Merged", "Running", "Not running", "Idle slots"):
        assert word in body, word
    assert "mr.days[0]" in body
    assert "ET" in body or "Eastern" in body
    assert "hand-off" in body


def test_merged_running_idle_slots_name_unread_hosts_and_the_table_cap_cause() -> None:
    body = _function("renderMergedRunning")
    assert "unread" in body
    assert "slots_source" in body
    assert "h.cause" in body


def test_merged_running_names_evidence_that_could_not_be_read() -> None:
    assert "evidence_unread" in _function("renderMergedRunning")


def test_merged_running_panel_text_is_not_below_twelve_pixels() -> None:
    for size in re.findall(r"font(?:-size)?:(?:[0-9]+ )?([0-9]*\.?[0-9]+)rem", CSS):
        assert float(size) >= 0.75, size
