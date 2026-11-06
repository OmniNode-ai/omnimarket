# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Machines tab draws one card per host of floor.host_status and never draws a missing or aged source as healthy (OMN-22673).

The records here are the compute node's own output (tests/nodes/node_factory_floor_compute/test_host_status.py builds them
from a recorded pool read), serialised and handed to the page script, so the chain is compute record to drawn card.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from tests.nodes.node_factory_floor_compute.test_host_status import (
    TABLE,
    full_facts,
    stale_facts,
    statuses,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    SHELL_FILES,
    SHELL_RECORD,
    _run_shell,
    _shell_out,
)

NODE_DIR = Path(__file__).resolve().parents[1]
SHELL_SRC = (NODE_DIR / "page/detail.html").read_text(encoding="utf-8")


def _record(**facts: object) -> dict[str, Any]:
    record = copy.deepcopy(SHELL_RECORD)
    record["floor"]["host_status"] = [
        s.model_dump(mode="json") for s in statuses(**facts).values()
    ]
    return record


def _machines(record: dict[str, Any]) -> str:
    return _shell_out(_run_shell({**SHELL_FILES, "factory-floor.json": record}))[
        "machines"
    ]


def _cards(html: str) -> dict[str, str]:
    parts = re.split(r'(?=<article class="hcard)', html)[1:]
    return {re.search(r'aria-label="([^"]+)"', p).group(1): p for p in parts}  # type: ignore[union-attr]


def test_the_machines_tab_is_declared_with_its_own_pane() -> None:
    assert '{id: "machines", label: "Machines", short: "Machines"}' in SHELL_SRC
    assert 'id="pane-machines"' in SHELL_SRC


def test_golden_every_host_gets_a_card_with_its_slots_lanes_and_health() -> None:
    cards = _cards(_machines(_record(**full_facts())))
    assert sorted(cards) == ["h053", "h101", "h106", "h200 (the Mac)", "h201"]
    h053 = cards["h053"]
    assert "of <b>16</b> lane slots used" in h053
    assert "<b>4</b>" in h053
    assert "floor-build" in h053
    assert "OMN-22478" in h053
    assert "landing-L28625" in h053
    assert "Free disk (run volume)" in h053
    assert "39.1 GB" in h053
    assert 'Docker</span><span class="v"' in h053
    assert "present" in h053
    assert 'class="pill ok">Read' in h053
    assert "out of lane placement" in cards["h200 (the Mac)"]
    assert "Out of selection" in cards["h200 (the Mac)"]
    assert "Refusing new work" in cards["h101"]
    assert "HOOK-PARSE-UNKNOWN" in cards["h101"]


def test_error_missing_sources_draw_unread_never_a_number_or_read() -> None:
    html = _machines(_record(host_table=TABLE))
    cards = _cards(html)
    assert len(cards) == 5
    assert "unread (the pool read was not recorded)" in cards["h053"]
    assert "unread (the host-drift fleet projection was not read)" in cards["h053"]
    assert "unread (the slot markers were not read)" in cards["h053"]
    assert 'class="pill ok"' not in html
    assert "Not read" in cards["h053"]


def test_stale_an_aged_reading_is_marked_stale_and_the_host_is_not_read() -> None:
    cards = _cards(_machines(_record(**stale_facts())))
    h053 = cards["h053"]
    assert "Reading out of date" in h053
    assert 'class="pill warn"' in h053
    assert h053.count("stale</span>") >= 3
    assert 'class="pill ok"' not in h053


def test_a_record_written_before_host_status_says_so() -> None:
    html = _machines(copy.deepcopy(SHELL_RECORD))
    assert "carries no per-host status" in html


def test_the_tab_lamp_is_amber_when_a_host_is_not_read_fresh() -> None:
    result = _run_shell({**SHELL_FILES, "factory-floor.json": _record(**stale_facts())})
    out = _shell_out(result)
    assert "Needs a look" in out["word-machines"]
    # Even with fresh sources the lamp is amber while one host refuses work (h101) and another has no reading (h106).
    fresh = _run_shell({**SHELL_FILES, "factory-floor.json": _record(**full_facts())})
    assert "Needs a look" in _shell_out(fresh)["word-machines"]
