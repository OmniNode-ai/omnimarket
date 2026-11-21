# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stage two of the memory pilot: the ruling question set is frozen, and the daily run can name Graphiti as its candidate."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from omnimarket.nodes.node_memory_pilot_effect.handlers import (
    handler_memory_pilot_run as run_module,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.frozen_pilot import (
    PilotNotFrozenError,
    verify_frozen,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.handler_memory_pilot_run import (
    HandlerMemoryPilotRun,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.pilot_scoring import (
    ABSTAIN_CATEGORY,
    HARD_CATEGORIES,
    load_questions,
    score_answer,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    EnumMemoryPilotBackend,
    ModelMemoryPilotOverlay,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot_run import (
    ModelMemoryPilotRunRequest,
)

from .conftest import PILOT_DIR, RecordingBus, RecordingRecorder
from .pilot_run_support import (
    CORR,
    NOW,
    FakeAnswerer,
    FakeAudit,
    FakeCandidate,
    FakeSink,
    Ticker,
    make_run_overlay,
)

pytestmark = pytest.mark.unit
STAGE2_DIR = PILOT_DIR.parent / "stage2_rulings"
GRAPHITI_UPDATE = {
    "backend": EnumMemoryPilotBackend.GRAPHITI,
    "lab_max_in_flight": 2,
    "graphiti_chunk_chars": 60,
    "graphiti_num_results": 3,
}


def _stage2_hash() -> str:
    return hashlib.sha256((STAGE2_DIR / "FROZEN.sha256").read_bytes()).hexdigest()


def test_stage2_is_frozen_and_a_changed_question_breaks_the_freeze(
    tmp_path: Path,
) -> None:
    assert verify_frozen(STAGE2_DIR, _stage2_hash()) == _stage2_hash()
    copy = tmp_path / "stage2"
    shutil.copytree(STAGE2_DIR, copy)
    changed = json.loads((copy / "questions.json").read_text(encoding="utf-8"))
    changed["questions"][0]["gold"] = "changed after the freeze"
    (copy / "questions.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(PilotNotFrozenError, match=r"questions\.json changed"):
        verify_frozen(copy, _stage2_hash())


def test_stage2_questions_have_known_answers_and_the_shape_the_bar_names() -> None:
    questions = load_questions(STAGE2_DIR).questions
    categories = [q.category for q in questions]
    assert len(questions) == 12
    assert (
        sum(c in HARD_CATEGORIES for c in categories) == 4
    )  # B2 names 4 hard questions
    assert categories.count(ABSTAIN_CATEGORY) == 2
    assert "planted" not in categories  # B3 is not applicable, and the bar says so
    bar = json.loads((STAGE2_DIR / "bar.json").read_text(encoding="utf-8"))
    assert "4 hard questions" in bar["bar"]["B2_hard_subset"]
    assert "not applicable" in bar["bar"]["B3_planted"]
    ids = [q.id for q in questions]
    assert len(set(ids)) == len(ids)


def test_stage2_gold_answers_score_right_and_a_wrong_or_empty_answer_scores_wrong() -> (
    None
):
    questions = load_questions(STAGE2_DIR)
    for question in questions.questions:
        if question.category == ABSTAIN_CATEGORY:
            assert score_answer(question, "NOT IN CONTEXT", questions.abstain_regex)
            assert not score_answer(question, "", questions.abstain_regex)
            assert not score_answer(question, "NOT IN CONTEXT", "never matches here")
        else:
            assert score_answer(question, question.gold, questions.abstain_regex), (
                question.id
            )
            assert not score_answer(question, "", questions.abstain_regex), question.id
            assert not score_answer(
                question, "NOT IN CONTEXT", questions.abstain_regex
            ), question.id


def test_stage2_manifest_lists_each_file_once_with_a_digest() -> None:
    manifest = json.loads((STAGE2_DIR / "corpus_manifest.json").read_text("utf-8"))
    paths = [entry["path"] for entry in manifest["files"]]
    assert len(paths) == len(set(paths)) == manifest["file_count"] == 18
    assert all(len(entry["sha256"]) == 64 for entry in manifest["files"])
    assert sum(e["bytes"] for e in manifest["files"]) == manifest["total_bytes"]


class _ClosingCandidate(FakeCandidate):
    name = "graphiti"

    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


async def _handle(
    overlay: ModelMemoryPilotOverlay,
    monkeypatch: pytest.MonkeyPatch,
    candidate: _ClosingCandidate,
    *,
    fail_ingest: bool = False,
) -> list[ModelMemoryPilotOverlay]:
    """Run the handler with no injected factory; return the overlays the Graphiti builder was handed."""
    built: list[ModelMemoryPilotOverlay] = []

    def make_graphiti(
        used: ModelMemoryPilotOverlay, _answerer: object, _prompt: str
    ) -> _ClosingCandidate:
        built.append(used)
        return candidate

    monkeypatch.setattr(run_module, "_make_graphiti", make_graphiti)
    candidate.fail_ingest = fail_ingest
    handler = HandlerMemoryPilotRun(
        event_bus=RecordingBus(),
        overlay=overlay,
        answerer=FakeAnswerer(),
        result_sink=FakeSink(),
        connection_audit=FakeAudit(),
        failure_recorder=RecordingRecorder(),
        clock=lambda: NOW,
        monotonic=Ticker(),
    )
    result = await handler.handle(ModelMemoryPilotRunRequest(correlation_id=CORR))
    assert result.status == ("failed" if fail_ingest else "completed"), (
        result.error_code,
        result.error_detail,
    )
    return built


async def test_a_graphiti_overlay_runs_graphiti_as_the_candidate_and_closes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = make_run_overlay(tmp_path).model_copy(update=GRAPHITI_UPDATE)
    candidate = _ClosingCandidate()
    built = await _handle(overlay, monkeypatch, candidate)
    assert len(built) == 1
    assert built[0].backend is EnumMemoryPilotBackend.GRAPHITI
    assert candidate.closed == 1


async def test_the_candidate_is_closed_when_its_ingest_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = make_run_overlay(tmp_path).model_copy(update=GRAPHITI_UPDATE)
    candidate = _ClosingCandidate()
    await _handle(overlay, monkeypatch, candidate, fail_ingest=True)
    assert candidate.closed == 1


async def test_control_a_cognee_overlay_never_builds_graphiti(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control of the selection: the same handler with the Cognee overlay asks for Cognee, not Graphiti."""
    overlay = make_run_overlay(tmp_path)
    assert overlay.backend is EnumMemoryPilotBackend.COGNEE
    cognee_built: list[ModelMemoryPilotOverlay] = []
    graphiti_built: list[ModelMemoryPilotOverlay] = []
    candidate = _ClosingCandidate()

    def make_cognee(used: ModelMemoryPilotOverlay, _prompt: str) -> _ClosingCandidate:
        cognee_built.append(used)
        return candidate

    def make_graphiti(
        used: ModelMemoryPilotOverlay, _answerer: object, _prompt: str
    ) -> _ClosingCandidate:
        graphiti_built.append(used)
        return candidate

    monkeypatch.setattr(run_module, "_make_cognee", make_cognee)
    monkeypatch.setattr(run_module, "_make_graphiti", make_graphiti)
    handler = HandlerMemoryPilotRun(
        event_bus=RecordingBus(),
        overlay=overlay,
        answerer=FakeAnswerer(),
        result_sink=FakeSink(),
        connection_audit=FakeAudit(),
        failure_recorder=RecordingRecorder(),
        clock=lambda: NOW,
        monotonic=Ticker(),
    )
    await handler.handle(ModelMemoryPilotRunRequest(correlation_id=CORR))
    assert len(cognee_built) == 1
    assert graphiti_built == []
