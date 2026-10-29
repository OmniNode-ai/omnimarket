# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21396: experiment STATUS rows replay into per-engine throughput notes."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.handler_engine_ladder import load_engine_ladder
from omnimarket.models.model_engine_experiment import (
    ModelEngineExperimentRow,
    format_experiment_line,
    parse_experiment_rows,
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


def row(**updates: Any) -> ModelEngineExperimentRow:
    return ModelEngineExperimentRow.model_validate(
        {
            "lane": "fix-1",
            "ticket": "OMN-21396",
            "attempt": 1,
            "engine": "code-edit",
            "model": "qwen",
            "outcome": "failed",
            "failure_cause": "checks-red",
            "duration_s": 120,
            "next_engine": "glm",
            **updates,
        }
    )


@pytest.mark.parametrize("ticket", [None, "OMN-21396"])
def test_experiment_format_parse_round_trip(ticket: str | None) -> None:
    original = row(ticket=ticket)
    stamp = "2026-10-25T02:40:00Z"
    assert original.cells() == [
        "kind=experiment",
        "attempt=1",
        "engine=code-edit",
        "model=qwen",
        "outcome=failed",
        "failure_cause=checks-red",
        "duration_s=120",
        "next_engine=glm",
    ]
    assert (
        original.text() == "attempt 1 on code-edit (qwen) failed: checks-red; next glm"
    )
    [parsed] = parse_experiment_rows(format_experiment_line(original, stamp))
    assert parsed == original.model_copy(update={"at": stamp})
    assert (
        row(
            attempt=2,
            engine="glm",
            model="glm-5.3",
            outcome="done",
            failure_cause="none",
            next_engine="none",
        ).text()
        == "attempt 2 on glm (glm-5.3) done; no next engine"
    )


def test_failure_cause_is_sanitised_at_construction() -> None:
    original = row(failure_cause="checks | red\n  retry\tplease")
    assert original.failure_cause == "checks-/-red-retry-please"
    assert (
        parse_experiment_rows(format_experiment_line(original, "2026-10-25T02:40:00Z"))[
            0
        ].failure_cause
        == original.failure_cause
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"engine": "other"},
        {"next_engine": "other"},
        {"attempt": 0},
        {"duration_s": -1},
        {"model": ""},
        {"outcome": ""},
        {"lane": ""},
        {"at": "yesterday"},
        {"at": "2026-02-30T00:00:00Z"},
        {"model": "qwen\nforged"},
        {"model": "qwen | forged"},
        {"extra": "forbidden"},
    ],
)
def test_experiment_rejects_invalid_fields(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        row(**updates)


def test_parser_ignores_unrelated_extra_cells_and_invalid_rows() -> None:
    valid = format_experiment_line(row(), "2026-10-25T02:40:00Z")
    ledger = "\n".join(
        [
            valid.replace(
                " | kind=", " | principal=agent | req=1 | via=runner | kind="
            ),
            valid.replace(" | STATUS |", " | CLAIM |"),
            valid.replace("kind=experiment", "kind=ask"),
            valid.replace("attempt=1", "attempt=bad"),
            valid.replace("engine=code-edit", "engine=unknown"),
            valid.replace("2026-10-25T02:40:00Z", "bad-timestamp"),
            "malformed",
            "STATUS | kind=experiment",
        ]
    )
    assert parse_experiment_rows(ledger) == [row(at="2026-10-25T02:40:00Z")]


def test_ac3_ac4_two_attempt_ledger_replay(tmp_path: Path) -> None:
    handler = HandlerRemoteLanePlacement()
    payload: dict[str, Any] = {
        "engine": "claude_sonnet",
        "readings": [
            {
                "name": "h201",
                "engines": ["claude_sonnet"],
                "lane_slots": 2,
                "lane_cap": 4,
                "rank_free": 10.0,
            }
        ],
        "ladder": {
            "lane": "fix-1",
            "ticket": "OMN-21396",
            "risk_class": "small-fix",
            "code_edit_ready": True,
            "local_model_slots": 1,
            "ceiling": "sonnet",
            "attempts": [],
        },
    }
    assert (
        handler.handle(ModelRemoteLanePlacementRequest.model_validate(payload)).rung
        == "code-edit"
    )
    payload["ladder"]["attempts"].append(
        {
            "engine": "code-edit",
            "model": "qwen",
            "outcome": "failed",
            "failure_cause": "checks-red",
            "duration_s": 120,
        }
    )
    second = handler.handle(ModelRemoteLanePlacementRequest.model_validate(payload))
    assert second.rung == "glm"
    assert second.experiment is not None
    ledger = tmp_path / "ledger"
    ledger.write_text(
        "2026-10-25T02:00:00Z | CLAIM | lane=other\n2026-10-25T02:01:00Z | STATUS | kind=ask\n"
        + format_experiment_line(second.experiment, "2026-10-25T02:40:00Z")
        + "\n"
    )
    payload["ladder"]["attempts"].append(
        {"engine": "glm", "model": "glm-5.3", "outcome": "done", "duration_s": 300}
    )
    third = handler.handle(ModelRemoteLanePlacementRequest.model_validate(payload))
    assert third.rung is None
    assert third.experiment is not None
    with ledger.open("a") as stream:
        stream.write(
            format_experiment_line(third.experiment, "2026-10-25T02:46:00Z") + "\n"
        )
    rows = parse_experiment_rows(ledger.read_text())
    assert [r.model_dump() for r in rows] == [
        row(at="2026-10-25T02:40:00Z").model_dump(),
        row(
            attempt=2,
            engine="glm",
            model="glm-5.3",
            outcome="done",
            failure_cause="none",
            duration_s=300,
            next_engine="none",
            at="2026-10-25T02:46:00Z",
        ).model_dump(),
    ]
    result = HandlerThroughputTickDecision().handle(
        ModelThroughputTickRequest.model_validate(
            {
                "now": "2026-10-25T03:00:00Z",
                "launchctl": "not_loaded",
                "engine_experiments": [r.model_dump() for r in rows]
                + [row(at="2026-10-24T02:59:59Z").model_dump()],
            }
        )
    )
    assert (
        "NOTE engine-experiments:code-edit | 24h attempts=1 successes=0 escalations=1"
        in result.lines
    )
    assert (
        "NOTE engine-experiments:glm | 24h attempts=1 successes=1 escalations=0"
        in result.lines
    )
    assert [
        line.split(" | ")[0]
        for line in result.lines
        if line.startswith("NOTE engine-experiments:")
    ] == [f"NOTE engine-experiments:{engine}" for engine in load_engine_ladder().rungs]
    assert "engine-experiments" in result.checked


def test_tick_window_boundary_undated_rows_and_optional_facts() -> None:
    handler = HandlerThroughputTickDecision()
    base = {"now": "2026-10-25T03:00:00Z", "launchctl": "not_loaded"}
    result = handler.handle(ModelThroughputTickRequest.model_validate(base))
    assert "engine-experiments" not in result.checked
    result = handler.handle(
        ModelThroughputTickRequest.model_validate(
            {
                **base,
                "engine_experiment_window_hours": 2,
                "engine_experiments": [
                    row(at="2026-10-25T01:00:00Z").model_dump(),
                    row().model_dump(),
                    row(at="2026-10-25T00:59:59Z").model_dump(),
                ],
            }
        )
    )
    assert (
        "NOTE engine-experiments:code-edit | 2h attempts=2 successes=0 escalations=2"
        in result.lines
    )
    assert (
        "NOTE engine-experiments:opus | 2h attempts=0 successes=0 escalations=0"
        in result.lines
    )


def test_harness_experiment_identity_round_trips_without_changing_placement_order():
    original = row(
        engine="local-agent",
        harness="crush",
        task_class="docs-edit",
        configuration="low90",
        item_key="docs-1",
    )
    [parsed] = parse_experiment_rows(
        format_experiment_line(original, "2026-10-26T12:00:00Z")
    )
    assert parsed.harness == "crush"
    assert parsed.task_class == "docs-edit"
    assert parsed.configuration == "low90"
    assert parsed.item_key == "docs-1"
    assert load_engine_ladder().rungs == ("code-edit", "glm", "codex", "sonnet", "opus")


def test_throughput_includes_an_observed_local_agent_without_a_new_placement_rung():
    from datetime import UTC, datetime

    from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
        Report,
        check_engine_experiments,
    )

    report = Report()
    request = ModelThroughputTickRequest(
        now="2026-10-26T12:00:00Z",
        launchctl="unavailable",
        engine_experiments=[
            row(engine="local-agent", outcome="done", failure_cause="none")
        ],
    )
    check_engine_experiments(report, request, datetime(2026, 10, 26, tzinfo=UTC))
    assert any(
        "engine-experiments:local-agent | 24h attempts=1 successes=1" in line
        for line in report.lines
    )
