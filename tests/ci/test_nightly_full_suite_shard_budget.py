# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The nightly's shards must finish inside their job budget, and a later run must never cancel one.

Measured on runs 59 to 70 (2026-11-14 to 2026-11-25): at ten shards, nine of ten shard jobs ended
`cancelled` at 30m17s (the job budget), the tenth passed at 29m17s, so no night completed, no junit
set was complete and no `nightly-attribution:<slot date>` record could be published.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/nightly-full-suite.yml"

# Lower bound of the measured full-suite shard time in minutes (10 shards x 30 min budget; nine of
# ten hit it), setup included. Per-shard work scales as this over the split count.
MEASURED_SHARD_MINUTES_TOTAL = 300
# The budget must hold at least this multiple of the expected per-shard time.
REQUIRED_HEADROOM = 1.5


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_shard_budget_holds_the_measured_shard_time_with_headroom() -> None:
    wf = _workflow()
    split_count = int(wf["env"]["NIGHTLY_SPLIT_COUNT"])
    budget = wf["jobs"]["shard"]["timeout-minutes"]
    expected = MEASURED_SHARD_MINUTES_TOTAL / split_count
    assert budget >= expected * REQUIRED_HEADROOM, (
        f"shard budget {budget} min < {REQUIRED_HEADROOM} x measured {expected:.1f} min"
    )


def test_matrix_lists_every_split() -> None:
    wf = _workflow()
    split_count = int(wf["env"]["NIGHTLY_SPLIT_COUNT"])
    assert wf["jobs"]["shard"]["strategy"]["matrix"]["split"] == list(
        range(1, split_count + 1)
    )


def test_a_later_run_never_cancels_a_running_nightly() -> None:
    # PyYAML reads the bare key `on` as True; the concurrency block is top level either way.
    concurrency = _workflow()["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert "github.run_id" not in str(concurrency["group"])


def test_collect_is_followed_by_publish_of_the_attribution_record() -> None:
    contract = yaml.safe_load(
        (
            ROOT
            / "src/omnimarket/nodes/node_test_selector_miss_collect_effect/contract.yaml"
        ).read_text()
    )
    text = yaml.safe_dump(contract)
    assert "operations_in_order" in text
    ops = _find(contract, "operations_in_order")
    assert ops == ["collect_nightly", "publish_attribution_record"]


def _find(node: Any, key: str) -> Any:
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            found = _find(v, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find(v, key)
            if found is not None:
                return found
    return None
