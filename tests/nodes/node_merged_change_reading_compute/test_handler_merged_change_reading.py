# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merged-change reading: golden per run kind, one error chain per missing step, unread, determinism and a failing control."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_merged_change_reading import (
    ModelMcrBuildFact,
    ModelMcrChange,
    ModelMcrConfig,
    ModelMcrLoadedNode,
    ModelMcrPublished,
    ModelMcrRequest,
    ModelMcrRunner,
    ModelMcrRuntimeFacts,
    ModelMcrSubscription,
    ModelMcrUnitRun,
)
from omnimarket.nodes.node_merged_change_reading_compute.handlers.handler_merged_change_reading import (
    HandlerMergedChangeReading,
)

CONTRACT = (
    Path(__file__).parents[3]
    / "src/omnimarket/nodes/node_merged_change_reading_compute/contract.yaml"
)
NOW = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)
STARTED = datetime(2026, 11, 14, 8, 56, 27, tzinfo=UTC)
AFTER = STARTED + timedelta(minutes=10)
BEFORE = STARTED - timedelta(minutes=10)


def _config() -> ModelMcrConfig:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return ModelMcrConfig.model_validate(document["merged_change_reading"])


def _runner(number: int, kind: str, **fields: Any) -> ModelMcrRunner:
    return ModelMcrRunner(repo="omnimarket", number=number, kind=kind, **fields)


def _request(
    runners: list[ModelMcrRunner],
    *,
    runtime: ModelMcrRuntimeFacts,
    in_build: dict[int, bool] | None = None,
    build_unread: tuple[str, ...] = (),
    changes: list[int] | None = None,
    config: ModelMcrConfig | None = None,
) -> ModelMcrRequest:
    numbers = changes if changes is not None else [r.number for r in runners]
    flags = in_build or {}
    return ModelMcrRequest(
        now=NOW,
        config=config or _config(),
        build_started_at=STARTED,
        changes=tuple(
            ModelMcrChange(repo="omnimarket", number=n, merge_sha=f"sha{n}")
            for n in numbers
        ),
        runners=tuple(runners),
        build_facts=tuple(
            ModelMcrBuildFact(
                repo="omnimarket", number=n, in_build=flags.get(n, True), source="test"
            )
            for n in numbers
            if n in flags or not build_unread
        ),
        build_unread=build_unread,
        runtime=runtime,
    )


def _scheduled(number: int = 1) -> ModelMcrRunner:
    return _runner(
        number,
        "schedule",
        nodes=("node_a",),
        trigger_topics=("onex.intent.platform.runtime-tick.v1",),
        result_topics=("onex.evt.omnimarket.a-completed.v1",),
    )


def _full_runtime() -> ModelMcrRuntimeFacts:
    return ModelMcrRuntimeFacts(
        loaded_nodes=(ModelMcrLoadedNode(node="node_a", loaded_at=AFTER),),
        subscriptions=(
            ModelMcrSubscription(
                node="node_a",
                topic="onex.intent.platform.runtime-tick.v1",
                joined_at=AFTER,
            ),
        ),
        published=(
            ModelMcrPublished(
                topic="onex.evt.omnimarket.a-completed.v1", last_at=AFTER, count=3
            ),
        ),
    )


def test_golden_every_run_kind_reads_running() -> None:
    runtime = ModelMcrRuntimeFacts(
        loaded_nodes=tuple(
            ModelMcrLoadedNode(node=n, loaded_at=AFTER)
            for n in ("node_a", "node_b", "node_c")
        ),
        subscriptions=(
            ModelMcrSubscription(node="node_a", topic="t.tick", joined_at=AFTER),
            ModelMcrSubscription(node="node_b", topic="t.cmd", joined_at=AFTER),
        ),
        published=(
            ModelMcrPublished(topic="t.a.done", last_at=AFTER, count=2),
            ModelMcrPublished(
                topic="t.b.done", last_at=AFTER + timedelta(minutes=1), count=1
            ),
            ModelMcrPublished(topic="t.c.done", last_at=AFTER, count=1),
        ),
        unit_runs=(
            ModelMcrUnitRun(unit="svc.service", last_run_at=AFTER, result="exit 0"),
            ModelMcrUnitRun(unit="lib.service", last_run_at=AFTER, result="exit 0"),
        ),
    )
    runners = [
        _runner(
            1,
            "schedule",
            nodes=("node_a",),
            trigger_topics=("t.tick",),
            result_topics=("t.a.done",),
        ),
        _runner(
            2,
            "topic",
            nodes=("node_b",),
            trigger_topics=("t.cmd",),
            result_topics=("t.b.done",),
        ),
        _runner(3, "route", nodes=("node_c",), result_topics=("t.c.done",)),
        _runner(4, "host_unit", unit="svc.service"),
        _runner(5, "overlay_value"),
        _runner(6, "library", unit="lib.service"),
    ]
    result = HandlerMergedChangeReading().handle(_request(runners, runtime=runtime))
    assert [r.verdict for r in result.readings] == ["SEEN_RUNNING"] * 6
    assert [r.number for r in result.readings] == [1, 2, 3, 4, 5, 6]
    assert (result.summary.total, result.summary.running) == (6, 6)
    assert result.readings[1].last_run_at == AFTER + timedelta(minutes=1)
    assert result.readings[1].last_result == "t.b.done"
    assert result.readings[3].last_result == "exit 0"
    assert result.readings[0].nodes[0].loaded is True
    assert result.readings[0].nodes[0].subscribed_topics == ("t.tick",)
    assert result.readings[0].missing_step == ""


def test_error_not_in_build() -> None:
    result = HandlerMergedChangeReading().handle(
        _request([_scheduled()], runtime=_full_runtime(), in_build={1: False})
    )
    reading = result.readings[0]
    assert (reading.verdict, reading.missing_step, reading.in_build) == (
        "NOT_RUNNING",
        "not_in_build",
        False,
    )
    assert reading.next_action == "roll dev with the merge"


def test_error_node_not_loaded() -> None:
    runtime = _full_runtime().model_copy(update={"loaded_nodes": ()})
    reading = (
        HandlerMergedChangeReading()
        .handle(_request([_scheduled()], runtime=runtime))
        .readings[0]
    )
    assert (reading.verdict, reading.missing_step) == ("NOT_RUNNING", "node_not_loaded")
    assert reading.nodes[0].loaded is False


def test_error_loaded_but_no_subscription() -> None:
    runtime = _full_runtime().model_copy(update={"subscriptions": ()})
    reading = (
        HandlerMergedChangeReading()
        .handle(_request([_scheduled()], runtime=runtime))
        .readings[0]
    )
    assert (reading.verdict, reading.missing_step) == (
        "NOT_RUNNING",
        "no_trigger_subscription",
    )


def test_error_runner_without_trigger_topic_is_not_subscribed() -> None:
    runner = _runner(1, "topic", nodes=("node_a",), result_topics=("t.x",))
    reading = (
        HandlerMergedChangeReading()
        .handle(_request([runner], runtime=_full_runtime()))
        .readings[0]
    )
    assert reading.missing_step == "no_trigger_subscription"


def test_error_only_run_is_older_than_the_build() -> None:
    runtime = _full_runtime().model_copy(
        update={
            "published": (
                ModelMcrPublished(
                    topic="onex.evt.omnimarket.a-completed.v1", last_at=BEFORE, count=9
                ),
            )
        }
    )
    reading = (
        HandlerMergedChangeReading()
        .handle(_request([_scheduled()], runtime=runtime))
        .readings[0]
    )
    assert (reading.verdict, reading.missing_step) == (
        "NOT_RUNNING",
        "no_run_since_build",
    )
    assert reading.last_run_at == BEFORE


def test_error_unit_last_run_is_older_than_the_build() -> None:
    runtime = ModelMcrRuntimeFacts(
        unit_runs=(
            ModelMcrUnitRun(unit="svc.service", last_run_at=BEFORE, result="exit 0"),
        )
    )
    reading = (
        HandlerMergedChangeReading()
        .handle(
            _request([_runner(4, "host_unit", unit="svc.service")], runtime=runtime)
        )
        .readings[0]
    )
    assert reading.missing_step == "no_run_since_build"


def test_error_ran_without_result_text_under_a_result_only_kind() -> None:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))[
        "merged_change_reading"
    ]
    document["run_kinds"] = [
        {"kind": "result_only", "requires": ["in_build", "result"]}
    ]
    config = ModelMcrConfig.model_validate(document)
    reading = (
        HandlerMergedChangeReading()
        .handle(
            _request(
                [
                    _runner(
                        1, "result_only", nodes=("node_a",), result_topics=("t.none",)
                    )
                ],
                runtime=ModelMcrRuntimeFacts(),
                config=config,
            )
        )
        .readings[0]
    )
    assert (reading.verdict, reading.missing_step) == ("NOT_RUNNING", "no_result")


def test_error_change_with_no_runner() -> None:
    result = HandlerMergedChangeReading().handle(
        _request([], runtime=_full_runtime(), changes=[7])
    )
    reading = result.readings[0]
    assert (reading.verdict, reading.missing_step, reading.kind) == (
        "NOT_RUNNING",
        "no_declared_runner",
        "",
    )
    assert result.summary.not_running == 1


def test_unread_group_reads_unread_never_not_running() -> None:
    runtime = _full_runtime().model_copy(
        update={"subscriptions": (), "unread": ("subscriptions",)}
    )
    reading = (
        HandlerMergedChangeReading()
        .handle(_request([_scheduled()], runtime=runtime))
        .readings[0]
    )
    assert reading.verdict == "UNREAD"
    assert reading.missing_step == ""
    assert "subscriptions" in reading.detail


def test_unread_build_with_no_fact_reads_unread() -> None:
    result = HandlerMergedChangeReading().handle(
        _request([_scheduled()], runtime=_full_runtime(), build_unread=("omnimarket",))
    )
    assert result.readings[0].verdict == "UNREAD"
    assert result.readings[0].in_build is None
    assert result.summary.unread == 1


def test_unread_loaded_nodes_reports_loaded_none() -> None:
    runtime = _full_runtime().model_copy(update={"unread": ("loaded_nodes",)})
    reading = (
        HandlerMergedChangeReading()
        .handle(_request([_scheduled()], runtime=runtime))
        .readings[0]
    )
    assert reading.verdict == "UNREAD"
    assert reading.nodes[0].loaded is None


def test_same_request_gives_same_result_in_any_order() -> None:
    runners = [_scheduled(1), _scheduled(2), _runner(3, "overlay_value")]
    runtime = _full_runtime()
    base = HandlerMergedChangeReading().handle(_request(runners, runtime=runtime))
    shuffled_runners = list(runners)
    random.Random(7).shuffle(shuffled_runners)
    again = HandlerMergedChangeReading().handle(
        _request(shuffled_runners, runtime=runtime, changes=[1, 2, 3])
    )
    assert base == again


def test_failing_control_the_config_order_decides_not_the_handler() -> None:
    """Drop ``subscribed`` from the schedule kind's requirements: the unsubscribed change flips to running.

    The control shows the verdict is read from the declared requirement list. A handler that ignored the list
    and always checked subscriptions would keep the not-running verdict and fail this test.
    """
    runtime = _full_runtime().model_copy(update={"subscriptions": ()})
    base = HandlerMergedChangeReading().handle(
        _request([_scheduled()], runtime=runtime)
    )
    assert base.readings[0].verdict == "NOT_RUNNING"
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))[
        "merged_change_reading"
    ]
    for kind in document["run_kinds"]:
        if kind["kind"] == "schedule":
            kind["requires"] = [r for r in kind["requires"] if r != "subscribed"]
    flipped = HandlerMergedChangeReading().handle(
        _request(
            [_scheduled()],
            runtime=runtime,
            config=ModelMcrConfig.model_validate(document),
        )
    )
    assert flipped.readings[0].verdict == "SEEN_RUNNING"


def test_invalid_requests_are_refused() -> None:
    handler = HandlerMergedChangeReading()
    with pytest.raises(ValueError, match="repeats a change"):
        handler.handle(
            _request([_scheduled()], runtime=_full_runtime(), changes=[1, 1])
        )
    with pytest.raises(ValueError, match="unknown run kind"):
        handler.handle(_request([_runner(1, "nope")], runtime=_full_runtime()))
    with pytest.raises(ValueError, match="timezone"):
        ModelMcrPublished(
            topic="t",
            last_at=datetime(2026, 11, 14, 9, 0),
            count=1,
        )


def test_config_refuses_an_undeclared_requirement() -> None:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))[
        "merged_change_reading"
    ]
    document["run_kinds"][0]["requires"] = ["in_build", "nonsense"]
    with pytest.raises(ValueError, match="undeclared"):
        ModelMcrConfig.model_validate(document)


def test_shared_result_topic_alone_is_unread_not_a_run() -> None:
    """Another node's publish on a shared topic must not read as this node having run."""
    runner = _runner(
        1,
        "schedule",
        nodes=("node_a",),
        trigger_topics=("onex.intent.platform.runtime-tick.v1",),
        shared_result_topics=("onex.evt.omnimarket.schedule-run.v1",),
    )
    runtime = _full_runtime().model_copy(
        update={
            "published": (
                ModelMcrPublished(
                    topic="onex.evt.omnimarket.schedule-run.v1", last_at=AFTER, count=9
                ),
            )
        }
    )
    reading = (
        HandlerMergedChangeReading()
        .handle(_request([runner], runtime=runtime))
        .readings[0]
    )
    assert reading.verdict == "UNREAD"
    assert "attribution" in reading.detail
    assert reading.last_run_at is None


def test_undeclared_evidence_reads_unread_and_a_verified_no_caller_reads_not_running() -> (
    None
):
    handler = HandlerMergedChangeReading()
    undeclared = _runner(1, "route", nodes=("node_a",))
    verified = _runner(2, "route", nodes=("node_a",), no_caller=True)
    result = handler.handle(_request([undeclared, verified], runtime=_full_runtime()))
    first, second = result.readings
    assert first.verdict == "UNREAD"
    assert "no run evidence" in first.detail
    assert (second.verdict, second.missing_step) == (
        "NOT_RUNNING",
        "no_run_since_build",
    )


def test_a_run_seen_beats_a_no_caller_flag() -> None:
    runner = _runner(
        1,
        "route",
        nodes=("node_a",),
        result_topics=("onex.evt.omnimarket.a-completed.v1",),
        no_caller=True,
    )
    reading = (
        HandlerMergedChangeReading()
        .handle(_request([runner], runtime=_full_runtime()))
        .readings[0]
    )
    assert reading.verdict == "SEEN_RUNNING"


def test_a_unit_that_ran_and_failed_reads_not_running_with_its_own_step() -> None:
    runtime = ModelMcrRuntimeFacts(
        unit_runs=(
            ModelMcrUnitRun(
                unit="svc.service",
                last_run_at=AFTER,
                result="exit 3 exit-code",
                ok=False,
            ),
        )
    )
    reading = (
        HandlerMergedChangeReading()
        .handle(
            _request([_runner(4, "host_unit", unit="svc.service")], runtime=runtime)
        )
        .readings[0]
    )
    assert (reading.verdict, reading.missing_step) == ("NOT_RUNNING", "last_run_failed")
    assert reading.last_result == "exit 3 exit-code"
    assert reading.last_run_at == AFTER
