# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's red verdicts equal the landing controller's on heads the PR watcher recorded.

``recorded_heads.json`` holds the ``ci`` block of 19 heads the watcher read on 2026-10-10, chosen to cover every
distinct verdict combination those records produced, and for each head the verdicts of the controller's own
``landing_facts`` functions (run on that ``ci`` block before the rules moved here).
The node, asked the same questions through ``handle(request)``, must answer the same.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit.nodes.node_pr_lifecycle_triage_compute import (
    landing_rules_adapter as lf,
)

pytestmark = pytest.mark.unit

_DOC = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "landing_red_rules"
        / "recorded_heads.json"
    ).read_text()
)
_CASES: list[dict[str, Any]] = _DOC["cases"]


def _id(case: dict[str, Any]) -> str:
    cites = case["cites"]
    return f"{cites['repo']}#{cites['pr']}@{cites['head_sha'][:9]}"


def test_the_corpus_cites_distinct_real_heads_and_covers_the_red_classes() -> None:
    heads = {
        (c["cites"]["repo"], c["cites"]["pr"], c["cites"]["head_sha"]) for c in _CASES
    }
    assert len(_CASES) >= 15
    assert len(heads) == len(_CASES)
    assert all(len(c["cites"]["head_sha"]) == 40 for c in _CASES)
    classes = {
        c["expected"]["classify_red"][m]
        for c in _CASES
        for m in ("companion_open", "companion_merged")
    }
    assert {"product", "cascade", "reviewer_pool"} <= classes
    assert any(c["expected"]["cascade_only"] for c in _CASES)
    assert any(c["expected"]["stale_summary_of"] == "rerun" for c in _CASES)
    assert any(c["expected"]["cancelled_of"] for c in _CASES)
    assert any(c["expected"]["pending_required_of_unread"] for c in _CASES)


@pytest.mark.parametrize("case", _CASES, ids=_id)
def test_the_node_gives_the_controllers_verdicts_on_a_recorded_head(
    case: dict[str, Any],
) -> None:
    ci, red, want = case["ci"], case["red"], case["expected"]
    runs = ci["runs"]
    sha = ci["sha"]
    got_class = {
        "companion_open": lf.classify_red(red, runs, companion_merged=False),
        "companion_merged": lf.classify_red(red, runs, companion_merged=True),
        "edge_fired": lf.classify_red(
            red, runs, companion_merged=False, edge_fired=True
        ),
    }
    assert got_class == want["classify_red"]
    assert lf.cascade_only(red, runs) is want["cascade_only"]
    assert list(lf.cancelled_of(ci, red)) == want["cancelled_of"]
    rows_only = {k: v for k, v in ci.items() if k != "cancelled"}
    assert list(lf.cancelled_of(rows_only, red)) == want["cancelled_of_from_rows"]
    assert lf.stale_summary_of(ci, sha) == want["stale_summary_of"]
    assert (
        lf.pending_required_of(ci, sha, None, now=_DOC["now"])
        == want["pending_required_of_unread"]
    )
