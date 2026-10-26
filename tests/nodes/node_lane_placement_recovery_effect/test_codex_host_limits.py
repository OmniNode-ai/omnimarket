# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Zero-turn provider refusals bench only Codex and never become same-host retries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_lane_placement_recovery_effect.handlers.handler_codex_lane_limit import (
    HandlerCodexLaneLimit,
)
from omnimarket.nodes.node_lane_placement_recovery_effect.models.model_codex_lane_limit import (
    ModelCodexLaneLimitRequest,
)
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    ModelRemoteLanePlacementRequest,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 26, 14, tzinfo=UTC)
ERROR = "exceeded retry limit, last status: 429 Too Many Requests"


def receipt(tmp_path: Path, **updates: object) -> Path:
    p = tmp_path / "run.json"
    p.write_text(
        json.dumps(
            {
                "engine": "codex",
                "model": "codex",
                "host": "h105",
                "run_id": "run",
                "final": True,
                "status": "failed",
                "num_turns": 0,
                "ended_at": "2026-10-26T14:00:00Z",
                "result": ERROR,
                **updates,
            }
        )
    )
    return p


def apply(tmp_path: Path, p: Path):
    return HandlerCodexLaneLimit().handle(
        ModelCodexLaneLimitRequest(
            receipt_path=str(p),
            placement_directory=str(tmp_path / "placements"),
            now=NOW,
        )
    )


def test_marks_once_for_an_hour_and_disables_same_host_retry(tmp_path: Path) -> None:
    p = receipt(tmp_path)
    result = apply(tmp_path, p)
    assert result.limited_until == "2026-10-26T15:00:00Z"
    marker = tmp_path / "placements/.codex-limited-h105"
    assert json.loads(marker.read_text())["until"] == result.limited_until
    failure = json.loads(p.read_text())["failure"]
    assert failure["kind"] == "codex-429"
    assert failure["retryable"] is False
    assert failure["avoid_hosts"] == ["h105"]
    assert apply(tmp_path, p).limited_until == result.limited_until
    assert not (tmp_path / "placements/.limited-h105").exists()


@pytest.mark.parametrize(
    ("error", "until"),
    [
        (ERROR + "; resets_at=1793025000", "2026-10-26T14:30:00Z"),
        (ERROR + "; reset_at=2026-10-26T16:00:00Z", "2026-10-26T16:00:00Z"),
        (ERROR + "; Retry-After: 120", "2026-10-26T14:02:00Z"),
        (
            ERROR + "; Retry-After: Mon, 26 Oct 2026 16:00:00 GMT",
            "2026-10-26T16:00:00Z",
        ),
        (ERROR + "; reset_at=invalid", "2026-10-26T15:00:00Z"),
    ],
)
def test_provider_reset_is_respected(tmp_path: Path, error: str, until: str) -> None:
    assert apply(tmp_path, receipt(tmp_path, result=error)).limited_until == until


@pytest.mark.parametrize(
    "updates",
    [
        {"num_turns": 1},
        {"model": "sonnet", "engine": "claude"},
        {"result": "file has 429 lines"},
        {"final": False},
        {"host": "../escape"},
        {"num_turns": None},
    ],
)
def test_unrelated_or_unproven_failures_do_not_mark(
    tmp_path: Path, updates: dict
) -> None:
    assert apply(tmp_path, receipt(tmp_path, **updates)).limited_until is None
    assert not (tmp_path / "placements/.codex-limited-h105").exists()


def test_reads_structured_error_from_out_and_ignores_quoted_tool_output(
    tmp_path: Path,
) -> None:
    p = receipt(tmp_path, result="no result")
    p.with_suffix(".out").write_text(
        json.dumps({"type": "item.completed", "item": {"text": ERROR}}) + "\n"
    )
    assert apply(tmp_path, p).limited_until is None
    with p.with_suffix(".out").open("a") as f:
        f.write(json.dumps({"type": "turn.failed", "error": {"message": ERROR}}) + "\n")
    assert apply(tmp_path, p).limited_until is not None


def test_expired_receipt_does_not_rebench_and_longer_mark_survives(
    tmp_path: Path,
) -> None:
    p = receipt(tmp_path, ended_at="2026-10-26T10:00:00Z")
    assert apply(tmp_path, p).limited_until is None
    p = receipt(tmp_path)
    marker = tmp_path / "placements/.codex-limited-h105"
    marker.parent.mkdir(exist_ok=True)
    marker.write_text(json.dumps({"until": "2026-10-26T18:00:00Z"}))
    assert apply(tmp_path, p).limited_until == "2026-10-26T18:00:00Z"


def test_placement_skips_codex_mark_but_claude_keeps_host() -> None:
    facts = {
        "codex_limited_hosts": ["h105"],
        "readings": [
            {
                "name": "h105",
                "rank_free": 100,
                "engines": ["claude_sonnet"],
                "lane_slots": 3,
            },
            {
                "name": "h201",
                "rank_free": 1,
                "engines": ["claude_sonnet"],
                "lane_slots": 3,
            },
        ],
    }
    handler = HandlerRemoteLanePlacement()
    assert (
        handler.handle(ModelRemoteLanePlacementRequest(engine="codex", **facts)).host
        == "h201"
    )
    assert (
        handler.handle(
            ModelRemoteLanePlacementRequest(engine="claude_sonnet", **facts)
        ).host
        == "h105"
    )
    assert (
        handler.handle(
            ModelRemoteLanePlacementRequest(engine="codex", pinned_host="h105", **facts)
        ).host
        is None
    )


def test_tick_suppresses_marked_codex_slots_and_counts_per_host() -> None:
    result = HandlerThroughputTickDecision().handle(
        ModelThroughputTickRequest.model_validate(
            {
                "now": "2026-10-26T14:00:00Z",
                "launchctl": "not_loaded",
                "lab_headroom": {
                    "live_admission": True,
                    "codex_429_counts": {"h105": 3, "h201": 0},
                    "hosts": [
                        {
                            "name": "h105",
                            "codex_limited_mark": {
                                "state": "present",
                                "until": "2026-10-26T15:00:00Z",
                            },
                            "admission": {
                                "placed": 0,
                                "lanes": 0,
                                "cap": 4,
                                "slots": 4,
                                "claude_slots": 4,
                                "codex_slots": 4,
                            },
                        }
                    ],
                },
            }
        )
    )
    assert result.codex_headroom[0].free == 0
    assert "Codex usage limit" in result.codex_headroom[0].refusal
    assert any(
        "engines" in line and "codex_429=h105:3,h201:0" in line for line in result.lines
    )
    assert not any("dispatch up to 4 Codex" in line for line in result.lines)


def test_existing_node_observes_engine_return_before_any_retry(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from omnimarket.nodes.node_lane_placement_recovery_effect.node import (
        NodeLanePlacementRecoveryEffect,
    )

    # The node reads the wall clock, so the refusal must end now for its bench to be live.
    ended_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = receipt(tmp_path, final=False, ended_at=ended_at)
    args = SimpleNamespace(
        model="codex", run_id="run", receipt=str(p), lane="lane", placement="lab"
    )
    calls = []

    def execute(current, **kwargs):
        calls.append(current.model)
        return 76

    placement = SimpleNamespace(placement_dir=lambda: tmp_path / "placements")
    node = NodeLanePlacementRecoveryEffect(
        {"execute_once": execute, "landing_placement": placement}
    )
    assert node.execute(args) == 76
    assert calls == ["codex"]
    assert json.loads(p.read_text())["failure"]["retryable"] is False
    assert (tmp_path / "placements/.codex-limited-h105").exists()
    assert json.loads(p.read_text())["final"] is False


def test_tick_adapter_counts_actual_receipts_once_and_preserves_other_engine_facts(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from omnimarket.nodes.node_lane_placement_recovery_effect.node import (
        NodeLanePlacementRecoveryEffect,
    )

    p = receipt(tmp_path)
    placement = SimpleNamespace(placement_dir=lambda: tmp_path / "placements")
    node = NodeLanePlacementRecoveryEffect({"_load_placement": lambda: placement})
    facts = {
        "now": "2026-10-26T14:00:00Z",
        "lab_headroom": {
            "hosts": [{"name": "h105", "admission": {"glm_slots": 2}}],
            "receipts": [{"path": str(p)}, {"path": str(p)}],
        },
    }
    wire = node.tick_request(facts)
    assert wire["lab_headroom"]["codex_429_counts"] == {"h105": 1}
    assert (
        wire["lab_headroom"]["hosts"][0]["codex_limited_mark"]["until"]
        == "2026-10-26T15:00:00Z"
    )
    assert facts["lab_headroom"]["hosts"][0]["admission"]["glm_slots"] == 2


def test_placement_adapter_reads_codex_marks_without_touching_claude_marks(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from omnimarket.nodes.node_lane_placement_recovery_effect.node import (
        NodeLanePlacementRecoveryEffect,
    )

    placement = SimpleNamespace(
        placement_dir=lambda: tmp_path,
        codex_limited_hosts=lambda _directory: {"h105": "2026-10-26T15:00:00Z"},
    )
    node = NodeLanePlacementRecoveryEffect({"landing_placement": placement})
    original = {"engine": "codex", "limited_hosts": ["h201"]}
    assert node.placement_request(original) == {
        **original,
        "codex_limited_hosts": ["h105"],
    }
    assert "codex_limited_hosts" not in original


def test_structured_provider_reset_inside_error_message(tmp_path: Path) -> None:
    p = receipt(tmp_path, result="no result")
    message = (
        ERROR
        + " "
        + json.dumps(
            {"error": {"type": "usage_limit_reached", "resets_at": 1793025000}}
        )
    )
    p.with_suffix(".out").write_text(
        json.dumps({"type": "turn.failed", "error": {"message": message}}) + "\n"
    )
    assert apply(tmp_path, p).limited_until == "2026-10-26T14:30:00Z"


def test_node_dispatches_typed_limit_request(tmp_path: Path) -> None:
    from omnimarket.nodes.node_lane_placement_recovery_effect.node import (
        NodeLanePlacementRecoveryEffect,
    )

    p = receipt(tmp_path)
    result = NodeLanePlacementRecoveryEffect({}).handle(
        ModelCodexLaneLimitRequest(str(p), str(tmp_path / "placements"), NOW)
    )
    assert result.limited_until == "2026-10-26T15:00:00Z"


def test_tick_counts_nonzero_turn_429_without_benching_host(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from omnimarket.nodes.node_lane_placement_recovery_effect.node import (
        NodeLanePlacementRecoveryEffect,
    )

    p = receipt(tmp_path, num_turns=2)
    placement = SimpleNamespace(placement_dir=lambda: tmp_path / "placements")
    node = NodeLanePlacementRecoveryEffect({"_load_placement": lambda: placement})
    facts = {
        "now": "2026-10-26T14:00:00Z",
        "lab_headroom": {"hosts": [{"name": "h105"}], "receipts": [{"path": str(p)}]},
    }
    wire = node.tick_request(facts)
    assert wire["lab_headroom"]["codex_429_counts"] == {"h105": 1}
    assert "codex_limited_mark" not in wire["lab_headroom"]["hosts"][0]
    assert json.loads(p.read_text())["failure"]["retryable"] is False
    assert json.loads(p.read_text())["failure"]["avoid_hosts"] == ["h105"]


def test_tick_counts_managed_codex_attempt_once_after_final_engine_changed(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from omnimarket.nodes.node_lane_placement_recovery_effect.node import (
        NodeLanePlacementRecoveryEffect,
    )

    p = receipt(tmp_path)
    root = tmp_path / "root.json"
    root.write_text(
        json.dumps(
            {
                "model": "glm",
                "engine": "glm",
                "final": True,
                "ended_at": "2026-10-26T14:00:00Z",
                "failover": {"attempts": [{"receipt": str(p)}]},
            }
        )
    )
    placement = SimpleNamespace(placement_dir=lambda: tmp_path / "placements")
    node = NodeLanePlacementRecoveryEffect({"_load_placement": lambda: placement})
    wire = node.tick_request(
        {
            "now": "2026-10-26T14:00:00Z",
            "lab_headroom": {
                "hosts": [{"name": "h105"}],
                "receipts": [{"path": str(root)}, {"path": str(root)}],
            },
        }
    )
    assert wire["lab_headroom"]["codex_429_counts"] == {"h105": 1}


def test_past_provider_reset_does_not_invent_a_new_hour(tmp_path: Path) -> None:
    p = receipt(tmp_path, result=ERROR + "; reset_at=2026-10-26T13:59:00Z")
    assert apply(tmp_path, p).limited_until is None
    assert json.loads(p.read_text())["failure"]["until"] == "2026-10-26T13:59:00Z"
    assert not (tmp_path / "placements/.codex-limited-h105").exists()


def test_marker_contains_no_raw_engine_error(tmp_path: Path) -> None:
    p = receipt(tmp_path, result=ERROR + "; private-error-canary")
    apply(tmp_path, p)
    marker = tmp_path / "placements/.codex-limited-h105"
    assert "private-error-canary" not in marker.read_text()
    assert "private-error-canary" not in json.dumps(
        json.loads(p.read_text())["failure"]
    )


def test_malformed_codex_mark_fails_closed_in_live_tick() -> None:
    result = HandlerThroughputTickDecision().handle(
        ModelThroughputTickRequest.model_validate(
            {
                "now": "2026-10-26T14:00:00Z",
                "launchctl": "not_loaded",
                "lab_headroom": {
                    "live_admission": True,
                    "hosts": [
                        {
                            "name": "h105",
                            "codex_limited_mark": {"state": "unreadable"},
                            "admission": {
                                "placed": 0,
                                "lanes": 0,
                                "cap": 4,
                                "slots": 4,
                                "claude_slots": 4,
                                "codex_slots": 4,
                            },
                        }
                    ],
                },
            }
        )
    )
    assert result.codex_headroom[0].free == 0
    assert result.codex_headroom[0].refusal == "Codex usage limit mark unreadable"
    assert any("up to 4 Claude lanes" in line for line in result.lines)


def test_cached_tick_honors_codex_mark_separately_from_claude() -> None:
    reading = "h105:cores=8,load1=1.0,free_gb=16.0,codex=login/ok,placed=0,cap=4,slots=4,login=none"
    result = HandlerThroughputTickDecision().handle(
        ModelThroughputTickRequest.model_validate(
            {
                "now": "2026-10-26T14:00:00Z",
                "launchctl": "not_loaded",
                "lab_headroom": {
                    "hosts": [
                        {
                            "name": "h105",
                            "codex_allowed": True,
                            "limited_mark": {
                                "state": "present",
                                "until": "2026-10-26T16:00:00Z",
                            },
                            "codex_limited_mark": {
                                "state": "present",
                                "until": "2026-10-26T15:00:00Z",
                            },
                            "parses": {reading: {"parsed": True}},
                        }
                    ],
                    "receipts": [
                        {
                            "started_at": "2026-10-26T13:59:00Z",
                            "host": "h105",
                            "readings": [reading],
                        }
                    ],
                },
            }
        )
    )
    assert result.codex_headroom[0].free == 0
    assert "Codex usage limit" in result.codex_headroom[0].refusal
    assert not any(
        "dispatch up to" in line and "Codex" in line for line in result.lines
    )


def test_successful_lane_quoting_429_is_not_a_metric_or_limit(tmp_path: Path) -> None:
    p = receipt(tmp_path, status="done", exit_code=0, num_turns=5)
    observed = apply(tmp_path, p)
    assert observed.observed_429 is False
    assert observed.limited_until is None
    assert "failure" not in json.loads(p.read_text())
