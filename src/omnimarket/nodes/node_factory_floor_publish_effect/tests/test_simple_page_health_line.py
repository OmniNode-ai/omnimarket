# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The main page draws the customer health line of the served record, with its own age (OMN-22923, piece 17b).

These tests run the main page's own script under node against a served floor record. Golden: a computed line is drawn with
its words and age. Error: a stale line is drawn red and marked untrustworthy, a line that is NOT WORKING is drawn red with
its named reading, and a record with no line says so. The failing control is the same page with the line taken out.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from omnimarket.nodes.node_factory_floor_publish_effect.tests.test_simple_page_machines import (
    SIMPLE,
    SIMPLE_SRC,
    _run,
)

NOW = "2026-11-06T18:40:00Z"


def _health(**changes: Any) -> dict[str, Any]:
    line: dict[str, Any] = {
        "schema_id": "omni.floor-health/1",
        "state": "WORKING",
        "line": "Customer path: WORKING, 4 readings, oldest reading 10 min",
        "generated_at": "2026-11-06T18:38:00Z",
        "max_age_s": 900,
        "readings": [],
        "named": [],
        "oldest_age_s": 600,
    }
    line.update(changes)
    return line


def _served(health: dict[str, Any] | None) -> dict[str, Any]:
    record = copy.deepcopy(SIMPLE)
    record["generated_at"] = "2026-11-06T18:39:00Z"
    if health is None:
        record.pop("health", None)
    else:
        record["health"] = health
    return {"floor-simple.json": record}


def _row(html: str) -> str:
    match = re.search(
        r'<div class="row(?: fp-tap)?" id="health-line"[^>]*>.*?</div></div>', html
    )
    assert match is not None, "the page drew no health line row"
    return match.group(0)


def test_golden_a_working_line_is_drawn_green_with_its_words_and_age() -> None:
    row = _row(_run(_served(_health()), now=NOW)["app"])
    assert "Customer path: WORKING, 4 readings, oldest reading 10 min" in row
    assert 'class="dot green"' in row
    assert "Computed 2 min ago, limit 15 min. Oldest reading 10 min ago." in row


def test_error_a_not_working_line_is_drawn_red_and_names_the_reading() -> None:
    line = "Customer path: NOT WORKING, piece 15 Delegation: stale, read 21 min ago, limit 15 min"
    health = _health(state="NOT WORKING", line=line, named=["piece 15 Delegation"])
    row = _row(_run(_served(health), now=NOW)["app"])
    assert line in row
    assert 'class="dot red"' in row


def test_error_an_unknown_line_is_drawn_amber() -> None:
    row = _row(
        _run(_served(_health(state="UNKNOWN", line="Customer path: UNKNOWN")), now=NOW)[
            "app"
        ]
    )
    assert 'class="dot amber"' in row


def test_error_a_working_line_older_than_its_own_limit_is_drawn_red_and_not_trusted() -> (
    None
):
    health = _health(generated_at="2026-11-06T18:20:00Z")
    row = _row(_run(_served(health), now=NOW)["app"])
    assert 'class="dot red"' in row
    assert "This line is itself 20 min ago, limit 15 min: do not trust it." in row


def test_error_a_record_without_the_line_says_so_and_never_draws_it_working() -> None:
    row = _row(_run(_served(None), now=NOW)["app"])
    assert "Customer health line: this record carries none." in row
    assert 'class="dot red"' in row
    assert "WORKING" not in row


def test_failing_control_the_page_without_the_line_draws_no_health_row() -> None:
    broken = SIMPLE_SRC.replace("  h += healthRow(doc.health);\n", "")
    assert broken != SIMPLE_SRC
    assert (
        'id="health-line"' not in _run(_served(_health()), src=broken, now=NOW)["app"]
    )
    assert 'id="health-line"' in _run(_served(_health()), now=NOW)["app"]
