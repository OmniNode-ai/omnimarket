# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21396 acceptance proof: one test per ticket criterion, each its falsifier.

AC1 start on the cheapest allowed rung with capacity, AC2 escalate one rung up
within the ROUTE ceiling and never below Claude for a Claude-required lane, AC3
one kind=experiment STATUS row per attempt, AC4 one throughput-tick line per
engine over 24 hours. The ledger is a temporary file; nothing touches the real
ledger, ~/.omninode or ~/.onex.
"""

from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.handler_engine_ladder import load_engine_ladder
from omnimarket.models.model_engine_experiment import (
    format_experiment_line,
    parse_experiment_rows,
)
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    ModelRemoteLanePlacementRequest,
    ModelRemoteLanePlacementResult,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)

pytestmark = pytest.mark.unit

LADDER_ORDER = ("code-edit", "glm", "codex", "sonnet", "opus")
EXPERIMENT_CELLS = (
    "lane=",
    "engine=",
    "model=",
    "outcome=",
    "failure_cause=",
    "duration_s=",
    "next_engine=",
)


def small_fix(**ladder: Any) -> dict[str, Any]:
    return {
        "engine": "claude_sonnet",
        "readings": [
            {
                "name": "mac-dev",
                "engines": ["claude_sonnet"],
                "lane_slots": 2,
                "lane_cap": 4,
                "rank_free": 10.0,
            }
        ],
        "ladder": {
            "lane": "ac-proof-1",
            "ticket": "OMN-21396",
            "risk_class": "small-fix",
            "code_edit_ready": True,
            "local_model_slots": 1,
            "ceiling": "sonnet",
            "attempts": [],
            **ladder,
        },
    }


def place(payload: dict[str, Any]) -> ModelRemoteLanePlacementResult:
    return HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest.model_validate(payload)
    )


def test_ac1_order_and_cheapest_rung_with_capacity() -> None:
    assert load_engine_ladder().rungs == LADDER_ORDER
    assert place(small_fix()).rung == "code-edit"
    no_local = place(small_fix(local_model_slots=0))
    assert no_local.rung == "glm"
    assert [(v.engine, v.reason) for v in no_local.rungs] == [
        ("code-edit", "no-local-slot"),
        ("glm", "chosen"),
    ]


@pytest.mark.parametrize("outcome", ["failed", "blocked-own-work", "checks-red"])
def test_ac2_failed_attempt_goes_one_rung_up(outcome: str) -> None:
    attempt = {"engine": "code-edit", "model": "qwen", "outcome": outcome}
    assert place(small_fix(attempts=[attempt])).rung == "glm"


def test_ac2_never_above_ceiling_and_claude_required_starts_on_claude() -> None:
    capped = place(
        small_fix(
            ceiling="glm",
            attempts=[{"engine": "glm", "model": "glm-5.3", "outcome": "failed"}],
        )
    )
    assert capped.rung is None
    assert {v.reason for v in capped.rungs} == {"above-route-ceiling"}
    pinned = place(small_fix(claude_required=True))
    assert pinned.rung == "sonnet"
    assert [(v.engine, v.reason) for v in pinned.rungs[:-1]] == [
        (rung, "claude-required") for rung in ("code-edit", "glm", "codex")
    ]


def two_attempt_ledger(ledger: Path) -> None:
    payload = small_fix()
    payload["ladder"]["attempts"].append(
        {
            "engine": "code-edit",
            "model": "qwen",
            "outcome": "failed",
            "failure_cause": "checks-red",
            "duration_s": 90,
        }
    )
    first = place(payload)
    assert first.rung == "glm"
    assert first.experiment is not None
    payload["ladder"]["attempts"].append(
        {"engine": "glm", "model": "glm-5.3", "outcome": "done", "duration_s": 240}
    )
    second = place(payload)
    assert second.rung is None
    assert second.experiment is not None
    ledger.write_text(
        "2026-10-31T10:00:00Z | CLAIM | lane=ac-proof-1 | ticket=OMN-21396\n"
        + format_experiment_line(first.experiment, "2026-10-31T10:05:00Z")
        + "\n2026-10-31T10:06:00Z | STATUS | lane=other | kind=ask\n"
        + format_experiment_line(second.experiment, "2026-10-31T10:10:00Z")
        + "\n"
    )


def test_ac3_two_attempts_write_exactly_two_experiment_rows(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    two_attempt_ledger(ledger)
    raw = [
        line for line in ledger.read_text().splitlines() if "kind=experiment" in line
    ]
    assert len(raw) == 2
    for line in raw:
        assert "| STATUS |" in line
        assert all(cell in line for cell in EXPERIMENT_CELLS), line
    rows = parse_experiment_rows(ledger.read_text())
    assert [(r.engine, r.outcome, r.next_engine) for r in rows] == [
        ("code-edit", "failed", "glm"),
        ("glm", "done", "none"),
    ]


def test_ac4_tick_prints_one_nonzero_line_per_engine(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    two_attempt_ledger(ledger)
    rows = parse_experiment_rows(ledger.read_text())
    result = HandlerThroughputTickDecision().handle(
        ModelThroughputTickRequest.model_validate(
            {
                "now": "2026-10-31T11:00:00Z",
                "launchctl": "not_loaded",
                "engine_experiments": [r.model_dump() for r in rows],
            }
        )
    )
    lines = [
        line for line in result.lines if line.startswith("NOTE engine-experiments:")
    ]
    assert [line.split(" | ")[0].split(":")[1] for line in lines] == list(LADDER_ORDER)
    assert (
        "NOTE engine-experiments:code-edit | 24h attempts=1 successes=0 escalations=1"
        in lines
    )
    assert (
        "NOTE engine-experiments:glm | 24h attempts=1 successes=1 escalations=0"
        in lines
    )
