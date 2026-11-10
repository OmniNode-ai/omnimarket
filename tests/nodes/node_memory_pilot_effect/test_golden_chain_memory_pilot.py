# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: node_memory_pilot_effect (OMN-19998), in-memory backend and fake lab model."""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_memory_pilot_effect.handlers.backend_in_memory import (
    BackendInMemory,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.handler_memory_pilot import (
    HandlerMemoryPilot,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.pilot_scoring import (
    load_questions,
    score_answer,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    EnumMemoryPilotOperation,
    EnumMemoryPilotReading,
    EnumMemoryPilotStatus,
    ModelMemoryPilotDocument,
    ModelMemoryPilotRequest,
)

from .conftest import CORR, PILOT_DIR, RecordingBus, RecordingRecorder, make_overlay

pytestmark = pytest.mark.unit


def _ingest(docs: tuple[ModelMemoryPilotDocument, ...]) -> ModelMemoryPilotRequest:
    return ModelMemoryPilotRequest(
        correlation_id=CORR,
        operation=EnumMemoryPilotOperation.INGEST,
        dataset="d",
        documents=docs,
    )


def _ask(question: str) -> ModelMemoryPilotRequest:
    return ModelMemoryPilotRequest(
        correlation_id=CORR,
        operation=EnumMemoryPilotOperation.QUERY,
        dataset="d",
        question=question,
    )


async def test_ingest_then_query_emits_started_then_completed_under_one_key(
    bus: RecordingBus,
    recorder: RecordingRecorder,
    planted_documents: tuple[ModelMemoryPilotDocument, ...],
) -> None:
    handler = HandlerMemoryPilot(
        event_bus=bus, failure_recorder=recorder, overlay=make_overlay()
    )
    ingested = await handler.handle(_ingest(planted_documents))
    assert ingested.status is EnumMemoryPilotStatus.COMPLETED
    assert ingested.reading is EnumMemoryPilotReading.WORKING
    assert ingested.documents_ingested == len(planted_documents) == 6
    answer = await handler.handle(
        _ask("What number fixes the sampling in the pilot that estimates spend?")
    )
    assert answer.status is EnumMemoryPilotStatus.COMPLETED
    assert "8123" in answer.answer
    assert bus.topics == ["started", "completed", "started", "completed"]
    assert [topic for topic, _k, _v in bus.events] == [
        "onex.evt.omnimarket.memory-pilot-started.v1",
        "onex.evt.omnimarket.memory-pilot-completed.v1",
        "onex.evt.omnimarket.memory-pilot-started.v1",
        "onex.evt.omnimarket.memory-pilot-completed.v1",
    ], "the events leave on the topics the contract declares"
    assert {key for _t, key, _v in bus.events} == {str(CORR).encode()}
    terminal = json.loads(bus.events[-1][2])
    assert terminal["status"] == "completed"
    assert terminal["reading"] == "WORKING"
    assert terminal["frozen_hash"] == answer.frozen_hash
    assert len(answer.frozen_hash) == 64
    assert recorder.rows == []


async def test_frozen_planted_questions_are_recalled_by_the_default_backend(
    bus: RecordingBus,
    recorder: RecordingRecorder,
    planted_documents: tuple[ModelMemoryPilotDocument, ...],
) -> None:
    """The six planted questions of the frozen set, asked of a store holding only the notes."""
    handler = HandlerMemoryPilot(
        event_bus=bus, failure_recorder=recorder, overlay=make_overlay()
    )
    await handler.handle(_ingest(planted_documents))
    questions = load_questions(PILOT_DIR)
    planted = [q for q in questions.questions if q.category == "planted"]
    assert len(planted) == 6
    right = 0
    for q in planted:
        result = await handler.handle(_ask(q.question))
        right += score_answer(
            q, "\n".join(r.excerpt for r in result.context), questions.abstain_regex
        )
    assert right >= 5, "planted facts must be recoverable from the retrieved context"


async def test_lexical_lab_backend_answers_from_the_lab_model_over_retrieved_context(
    bus: RecordingBus,
    recorder: RecordingRecorder,
    planted_documents: tuple[ModelMemoryPilotDocument, ...],
) -> None:
    seen: list[tuple[str, str, str]] = []

    class FakeAnswerer:
        async def answer(self, system_prompt: str, question: str, context: str) -> str:
            seen.append((system_prompt, question, context))
            return "fixed seed 8123"

    backend = BackendInMemory(
        name="lexical_lab", answerer=FakeAnswerer(), system_prompt="frozen"
    )
    handler = HandlerMemoryPilot(
        event_bus=bus,
        failure_recorder=recorder,
        overlay=make_overlay(),
        backend=backend,
    )
    await handler.handle(_ingest(planted_documents))
    result = await handler.handle(
        _ask("What number fixes the sampling in the pilot that estimates spend?")
    )
    assert result.answer == "fixed seed 8123"
    assert result.backend == "lexical_lab"
    assert seen
    assert seen[0][0] == "frozen"
    assert "planted_notes/PN-2-estimator-seed.txt" in seen[0][2]
