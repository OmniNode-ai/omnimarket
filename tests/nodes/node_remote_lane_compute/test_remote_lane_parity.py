# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity: the node decides what the remote-lane runner decided on the same facts (OMN-20669).

``fixtures/parity_cases.json`` was recorded by running the runner's own functions
(``eligible`` with ``choose_lane`` or ``choose_codex`` over real placement readings,
and ``closing_outcome``, ``final_lane_outcome``, ``parse_delegation_line`` and
``waiting_on_background``) on generated inputs. Each placement case carries the
readings' evaluated facts; the expected answer is the runner's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_remote_lane_close_compute.handlers import (
    HandlerRemoteLaneResult,
)
from omnimarket.nodes.node_remote_lane_close_compute.models import (
    ModelRemoteLaneResultRequest,
)
from omnimarket.nodes.node_remote_lane_compute.handlers import (
    HandlerRemoteLanePlacement,
    handler_remote_lane_placement,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    ModelRemoteLanePlacementRequest,
)

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "parity_cases.json").read_text()
)


def _placed(case: dict[str, Any]) -> dict[str, Any]:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest.model_validate(case["request"])
    )
    return {"host": result.host, "engine": result.engine, "local": result.local}


def test_the_recorded_cases_cover_every_branch() -> None:
    placement = CASES["placement"]
    assert len(placement) >= 400
    hosts = [c["expected"]["host"] for c in placement]
    assert sum(h is not None for h in hosts) >= 100
    assert sum(h is None for h in hosts) >= 100
    assert any(c["expected"]["local"] for c in placement)
    assert sum(c["request"]["engine"] == "codex" for c in placement) >= 100
    outcomes = {c["expected"]["outcome"] for c in CASES["result"]}
    assert outcomes >= {
        "failed",
        "rejected-no-delegation",
        "unknown",
        "done",
        "handed-off",
    }


@pytest.mark.parametrize("index", range(len(CASES["placement"])))
def test_placement_matches_the_runner(index: int) -> None:
    case = CASES["placement"][index]
    assert _placed(case) == case["expected"]


@pytest.mark.parametrize("index", range(len(CASES["result"])))
def test_closing_matches_the_runner(index: int) -> None:
    case = CASES["result"][index]
    result = HandlerRemoteLaneResult().handle(
        ModelRemoteLaneResultRequest.model_validate(case["request"])
    )
    assert (
        result.model_dump(exclude={"delegation"}) | {"delegation": result.delegation}
        == case["expected"]
    )


def test_a_broken_rank_fails_the_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: ranking by the least free capacity disagrees with the runner."""
    monkeypatch.setattr(
        handler_remote_lane_placement,
        "_spread_key",
        lambda r: (r.placed == 0, -r.rank_free, r.mem_avail_gb, r.local, r.name),
    )
    misses = sum(_placed(c) != c["expected"] for c in CASES["placement"])
    assert misses > 0
