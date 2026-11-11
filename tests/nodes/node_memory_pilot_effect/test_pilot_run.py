# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily memory pilot run (OMN-23619): golden chain, error chain, failing control, adapters and metrics."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.contract_topics import contract_topic
from omnimarket.nodes.node_memory_pilot_effect.handlers import (
    handler_memory_pilot_run as run_module,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.connection_audit import (
    SocketConnectionAudit,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.handler_memory_pilot_run import (
    HandlerMemoryPilotRun,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.journal_event_publisher import (
    JournalEventPublisher,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.pilot_run_metrics import (
    ModelQuestionTrial,
    p95_seconds,
    score_arm,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.result_file_sink import (
    FileResultSink,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelMemoryPilotOverlay,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot_run import (
    EnumMemoryPilotRunErrorCode as Code,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot_run import (
    ModelMemoryPilotRunRequest,
    ModelMemoryPilotRunResult,
)

from .conftest import RecordingBus, RecordingRecorder
from .pilot_run_support import (
    CORR,
    NAMES,
    NOW,
    FakeAnswerer,
    FakeAudit,
    FakeCandidate,
    FakeSink,
    Ticker,
    make_run_overlay,
)

pytestmark = pytest.mark.unit
CONTRACT = Path(run_module.__file__).resolve().parent.parent / "contract.yaml"
STARTED = contract_topic(CONTRACT, "publish_topics", "-run-started.v1")
COMPLETED = contract_topic(CONTRACT, "publish_topics", "-run-completed.v1")
FAILED = contract_topic(CONTRACT, "publish_topics", "-run-failed.v1")


async def _run(
    overlay: ModelMemoryPilotOverlay,
    *,
    candidate: FakeCandidate | None = None,
    answerer: FakeAnswerer | None = None,
    sink: FakeSink | None = None,
    audit: FakeAudit | None = None,
) -> tuple[ModelMemoryPilotRunResult, RecordingBus, RecordingRecorder, FakeSink]:
    bus, recorder, used_sink = RecordingBus(), RecordingRecorder(), sink or FakeSink()
    cand = candidate or FakeCandidate()
    handler = HandlerMemoryPilotRun(
        event_bus=bus,
        overlay=overlay,
        answerer=answerer or FakeAnswerer(),
        result_sink=used_sink,
        connection_audit=audit or FakeAudit(),
        candidate_factory=lambda _overlay, _prompt: cand,
        failure_recorder=recorder,
        clock=lambda: NOW,
        monotonic=Ticker(),
    )
    result = await handler.handle(ModelMemoryPilotRunRequest(correlation_id=CORR))
    return result, bus, recorder, used_sink


@pytest.mark.parametrize("parallel", [True, False])
async def test_golden_run_measures_both_arms_and_writes_the_result_once(
    tmp_path: Path, parallel: bool
) -> None:
    overlay = make_run_overlay(tmp_path, parallel=parallel)
    audit = FakeAudit()
    result, bus, recorder, sink = await _run(overlay, audit=audit)
    assert result.status == "completed"
    assert result.error_code is None
    assert [t for t, _k, _v in bus.events] == [STARTED, COMPLETED]
    assert {k for _t, k, _v in bus.events} == {str(CORR).encode()}
    assert recorder.rows == []
    assert audit.started_with == ("127.0.0.1",)
    ((written_at, metrics),) = sink.writes
    assert written_at == NOW
    assert set(metrics) == set(NAMES.values())
    # candidate answers all 7 right; the baseline only the 3 lexical/abstain ones of 7
    assert metrics[NAMES["overall_gain"]] == pytest.approx(1 - 3 / 7)
    assert metrics[NAMES["hard_gain"]] == pytest.approx(1.0)
    assert metrics[NAMES["planted_recalled"]] == pytest.approx(2.0)
    assert metrics[NAMES["abstain_margin"]] == pytest.approx(0.0)
    assert metrics[NAMES["failed_items"]] == 0.0
    assert metrics[NAMES["outside_connections"]] == 0.0
    assert result.result_written
    assert result.candidate is not None
    assert result.candidate.queries == 14  # 7 questions, 2 trials


async def test_control_a_candidate_that_never_answers_reads_a_negative_gain(
    tmp_path: Path,
) -> None:
    """The control of the golden case: the gain is the candidate's own answers, not a constant."""
    result, _bus, _rec, sink = await _run(
        make_run_overlay(tmp_path), candidate=FakeCandidate(good=False)
    )
    assert result.status == "completed"
    assert sink.writes[0][1][NAMES["overall_gain"]] < 0
    assert sink.writes[0][1][NAMES["planted_recalled"]] == 0.0


async def test_outside_connections_are_counted_into_the_result(tmp_path: Path) -> None:
    result, _b, _r, sink = await _run(
        make_run_overlay(tmp_path), audit=FakeAudit(("203.0.113.9:443",))
    )
    assert sink.writes[0][1][NAMES["outside_connections"]] == 1.0
    assert result.outside_connections == ("203.0.113.9:443",)


async def _failed(
    result: ModelMemoryPilotRunResult,
    bus: RecordingBus,
    rec: RecordingRecorder,
    code: Code,
) -> None:
    assert result.status == "failed"
    assert result.error_code is code
    assert [t for t, _k, _v in bus.events] == [STARTED, FAILED]
    assert [row[1] for row in rec.rows] == [code.value]


async def test_error_pilot_not_frozen_asks_no_model_and_writes_nothing(
    tmp_path: Path,
) -> None:
    overlay = make_run_overlay(tmp_path)
    (overlay.pilot_dir / "questions.json").write_text("{}", encoding="utf-8")
    result, bus, rec, sink = await _run(overlay)
    await _failed(result, bus, rec, Code.PILOT_NOT_FROZEN)
    assert sink.writes == []


async def test_error_endpoint_off_the_allow_list(tmp_path: Path) -> None:
    result, bus, rec, sink = await _run(
        make_run_overlay(tmp_path, allowed=("192.0.2.1",))
    )
    await _failed(result, bus, rec, Code.ENDPOINT_NOT_ALLOWED)
    assert sink.writes == []


async def test_error_a_corpus_file_that_differs_from_the_manifest(
    tmp_path: Path,
) -> None:
    overlay = make_run_overlay(tmp_path)
    assert overlay.run is not None
    (overlay.run.corpus_root / "adrs/ADR-0001.md").write_text(
        "changed", encoding="utf-8"
    )
    result, bus, rec, sink = await _run(overlay)
    await _failed(result, bus, rec, Code.CORPUS_MISMATCH)
    assert "ADR-0001" in result.error_detail
    assert sink.writes == []


@pytest.mark.parametrize("who", ["baseline", "candidate"])
async def test_error_lab_model_down_writes_no_result(tmp_path: Path, who: str) -> None:
    result, bus, rec, sink = await _run(
        make_run_overlay(tmp_path),
        answerer=FakeAnswerer(down=who == "baseline"),
        candidate=FakeCandidate(down=who == "candidate"),
    )
    await _failed(result, bus, rec, Code.LAB_MODEL_UNAVAILABLE)
    assert sink.writes == []
    assert not result.result_written


async def test_error_a_failed_ingest_writes_the_partial_result_the_bar_rejects(
    tmp_path: Path,
) -> None:
    result, bus, rec, sink = await _run(
        make_run_overlay(tmp_path), candidate=FakeCandidate(fail_ingest=True)
    )
    await _failed(result, bus, rec, Code.INGEST_FAILED)
    ((_, metrics),) = sink.writes
    assert set(metrics) == {
        NAMES["ingest_seconds"],
        NAMES["failed_items"],
        NAMES["outside_connections"],
    }
    assert metrics[NAMES["failed_items"]] == 4.0  # every document of the corpus
    assert result.result_written


async def test_error_a_result_that_cannot_be_written(tmp_path: Path) -> None:
    result, bus, rec, _sink = await _run(
        make_run_overlay(tmp_path), sink=FakeSink(fail=True)
    )
    await _failed(result, bus, rec, Code.RESULT_NOT_WRITTEN)
    assert not result.result_written


async def test_error_an_overlay_without_run_settings(tmp_path: Path) -> None:
    overlay = make_run_overlay(tmp_path).model_copy(update={"run": None})
    result, bus, rec, _sink = await _run(overlay)
    await _failed(result, bus, rec, Code.OVERLAY_INVALID)


def test_p95_is_nearest_rank_and_planted_is_per_question_mean() -> None:
    assert p95_seconds([float(n) for n in range(1, 21)]) == 19.0
    assert p95_seconds([]) == 0.0
    trials = [
        ModelQuestionTrial(
            question_id="P1",
            category="planted",
            trial=t,
            correct=c,
            latency_seconds=1.0,
        )
        for t, c in ((1, True), (2, False))
    ] + [
        ModelQuestionTrial(
            question_id="P2",
            category="planted",
            trial=1,
            correct=True,
            latency_seconds=1.0,
        )
    ]
    assert score_arm("x", trials).planted_mean == pytest.approx(1.5)


def test_result_file_is_written_whole_and_a_bad_path_raises(tmp_path: Path) -> None:
    target = tmp_path / "out" / "result.json"
    FileResultSink(target).write(NOW, {"a": 1.0})
    assert json.loads(target.read_text()) == {
        "measured_at": NOW.isoformat(),
        "metrics": {"a": 1.0},
    }
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(OSError, match=r"."):
        FileResultSink(blocker / "result.json").write(NOW, {})


async def test_journal_publisher_appends_parseable_lines(tmp_path: Path) -> None:
    publisher = JournalEventPublisher(tmp_path / "j" / "events.jsonl")
    await publisher.publish("t", b"k", b'{"x": 1}')
    await publisher.publish("t", None, b'{"x": 2}')
    lines = [
        json.loads(x) for x in (tmp_path / "j/events.jsonl").read_text().splitlines()
    ]
    assert [(r["topic"], r["key"], r["value"]) for r in lines] == [
        ("t", "k", {"x": 1}),
        ("t", None, {"x": 2}),
    ]


def test_connection_audit_counts_only_outside_connections_inside_a_run() -> None:
    audit = SocketConnectionAudit()
    sys.audit("socket.connect", None, ("203.0.113.9", 443))  # no run: ignored
    audit.start(("192.0.2.10",))
    sys.audit("socket.connect", None, ("203.0.113.9", 443))
    sys.audit("socket.connect", None, ("127.0.0.1", 9))
    sys.audit("socket.connect", None, ("192.0.2.10", 8000))
    sys.audit("socket.connect", None, "/run/sock")
    assert audit.stop() == ("203.0.113.9:443",)
    sys.audit("socket.connect", None, ("203.0.113.10", 1))
    audit.start(())
    assert audit.stop() == ()
