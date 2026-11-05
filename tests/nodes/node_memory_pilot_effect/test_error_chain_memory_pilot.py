# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: node_memory_pilot_effect (OMN-19998). A typed failure event, a failure row, NOT_WORKING."""

from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_memory_pilot_effect.handlers.backend_cognee import (
    BackendCognee,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.backend_in_memory import (
    BackendInMemory,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.handler_memory_pilot import (
    HandlerMemoryPilot,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.lab_reachability import (
    probe_endpoint,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    EnumMemoryPilotErrorCode as Code,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    EnumMemoryPilotOperation,
    EnumMemoryPilotReading,
    EnumMemoryPilotStatus,
    ModelLabEndpoint,
    ModelMemoryPilotDocument,
    ModelMemoryPilotOverlay,
    ModelMemoryPilotRequest,
    ModelMemoryPilotResult,
)
from omnimarket.nodes.node_memory_pilot_effect.protocols.protocol_memory_pilot import (
    BackendAnswer,
    LabModelUnavailableError,
)

from .conftest import (
    CORR,
    PILOT_DIR,
    RecordingBus,
    RecordingRecorder,
    frozen_hash,
    make_overlay,
)

pytestmark = pytest.mark.unit
DOC = ModelMemoryPilotDocument(doc_id="a.md", text="The seed is 8123.\n\nOther text.")


def _req(
    op: EnumMemoryPilotOperation = EnumMemoryPilotOperation.QUERY,
) -> ModelMemoryPilotRequest:
    if op is EnumMemoryPilotOperation.INGEST:
        return ModelMemoryPilotRequest(
            correlation_id=CORR, operation=op, dataset="d", documents=(DOC,)
        )
    return ModelMemoryPilotRequest(
        correlation_id=CORR, operation=op, dataset="d", question="seed number"
    )


def _assert_typed_failure(
    result: ModelMemoryPilotResult,
    bus: RecordingBus,
    recorder: RecordingRecorder,
    code: Code,
) -> None:
    assert result.status is EnumMemoryPilotStatus.FAILED
    assert result.error_code is code
    assert result.reading is EnumMemoryPilotReading.NOT_WORKING
    assert result.answer == ""
    assert result.documents_ingested == 0
    assert bus.topics == ["started", "failed"], (
        "exactly one terminal event, and it is the failure"
    )
    assert [topic for topic, _k, _v in bus.events] == [
        "onex.evt.omnimarket.memory-pilot-started.v1",
        "onex.evt.omnimarket.memory-pilot-failed.v1",
    ], "the failure leaves on the failed topic the contract declares"
    assert json.loads(bus.events[-1][2])["error_code"] == code.value
    assert [row[1] for row in recorder.rows] == [code.value]
    assert recorder.rows[0][0] == str(CORR)


async def test_lab_model_down_fails_fast_and_invents_no_answer(
    bus: RecordingBus, recorder: RecordingRecorder
) -> None:
    class DownAnswerer:
        async def answer(self, system_prompt: str, question: str, context: str) -> str:
            raise LabModelUnavailableError("ConnectionRefusedError")

    backend = BackendInMemory(
        name="lexical_lab", answerer=DownAnswerer(), system_prompt="p"
    )
    handler = HandlerMemoryPilot(
        event_bus=bus,
        failure_recorder=recorder,
        overlay=make_overlay(),
        backend=backend,
    )
    await backend.ingest("d", (DOC,))
    result = await handler.handle(_req())
    _assert_typed_failure(result, bus, recorder, Code.LAB_MODEL_UNAVAILABLE)


def test_probe_of_a_closed_port_is_lab_model_unavailable() -> None:
    endpoint = ModelLabEndpoint(
        base_url="http://127.0.0.1:9/v1", model="m", timeout_seconds=2
    )
    with pytest.raises(LabModelUnavailableError):
        probe_endpoint(endpoint)


def test_probe_of_a_listening_port_passes() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        endpoint = ModelLabEndpoint(
            base_url=f"http://127.0.0.1:{port}/v1", model="m", timeout_seconds=2
        )
        probe_endpoint(endpoint)


async def test_lexical_lab_without_an_injected_answerer_is_refused_not_faked(
    bus: RecordingBus, recorder: RecordingRecorder
) -> None:
    handler = HandlerMemoryPilot(
        event_bus=bus,
        failure_recorder=recorder,
        overlay=make_overlay(backend="lexical_lab"),
    )
    result = await handler.handle(_req(EnumMemoryPilotOperation.INGEST))
    _assert_typed_failure(result, bus, recorder, Code.BACKEND_ERROR)
    assert result.error_detail == "no lab answerer injected"


async def test_cognee_backend_with_the_lab_model_down_fails_before_touching_the_package(
    bus: RecordingBus, recorder: RecordingRecorder, tmp_path: Path
) -> None:
    overlay = ModelMemoryPilotOverlay.model_validate(
        {
            "backend": "cognee",
            "frozen_hash": frozen_hash(),
            "pilot_dir": PILOT_DIR,
            "telemetry_disabled": True,
            "allowed_hosts": ("127.0.0.1",),
            "chat": {
                "base_url": "http://127.0.0.1:9/v1",
                "model": "m",
                "timeout_seconds": 2,
            },
            "embeddings": {
                "base_url": "http://127.0.0.1:9/v1",
                "model": "e",
                "timeout_seconds": 2,
            },
            "embedding_dimensions": 8,
            "ingest_data_per_batch": 2,
            "storage_root": tmp_path,
        }
    )
    backend = BackendCognee(overlay, system_prompt="p")
    handler = HandlerMemoryPilot(
        event_bus=bus, failure_recorder=recorder, overlay=overlay, backend=backend
    )
    result = await handler.handle(_req(EnumMemoryPilotOperation.INGEST))
    _assert_typed_failure(result, bus, recorder, Code.LAB_MODEL_UNAVAILABLE)
    assert backend._cognee is None, "the package was never imported"


async def test_a_pilot_without_its_frozen_bar_does_not_start(
    bus: RecordingBus, recorder: RecordingRecorder
) -> None:
    wrong = HandlerMemoryPilot(
        event_bus=bus, failure_recorder=recorder, overlay=make_overlay(frozen="0" * 64)
    )
    _assert_typed_failure(
        await wrong.handle(_req(EnumMemoryPilotOperation.INGEST)),
        bus,
        recorder,
        Code.PILOT_NOT_FROZEN,
    )


async def test_a_question_changed_after_the_freeze_is_refused(
    bus: RecordingBus, recorder: RecordingRecorder, tmp_path: Path
) -> None:
    copy = tmp_path / "stage1"
    shutil.copytree(PILOT_DIR, copy)
    expected = frozen_hash(copy)
    ok = HandlerMemoryPilot(
        event_bus=bus,
        failure_recorder=recorder,
        overlay=make_overlay(pilot_dir=copy, frozen=expected),
    )
    assert (
        await ok.handle(_req(EnumMemoryPilotOperation.INGEST))
    ).status is EnumMemoryPilotStatus.COMPLETED
    questions = json.loads((copy / "questions.json").read_text())
    questions["questions"][0]["question"] += " (edited)"
    (copy / "questions.json").write_text(json.dumps(questions))
    bus2, rec2 = RecordingBus(), RecordingRecorder()
    broken = HandlerMemoryPilot(
        event_bus=bus2,
        failure_recorder=rec2,
        overlay=make_overlay(pilot_dir=copy, frozen=expected),
    )
    _assert_typed_failure(
        await broken.handle(_req(EnumMemoryPilotOperation.INGEST)),
        bus2,
        rec2,
        Code.PILOT_NOT_FROZEN,
    )


async def test_an_endpoint_off_the_allowed_hosts_is_refused_and_never_called(
    bus: RecordingBus, recorder: RecordingRecorder
) -> None:
    calls: list[str] = []

    class Spy:
        name = "spy"

        async def ingest(
            self, dataset: str, documents: tuple[ModelMemoryPilotDocument, ...]
        ) -> int:
            calls.append("ingest")
            return 1

        async def query(self, dataset: str, question: str) -> BackendAnswer:
            calls.append("query")
            return BackendAnswer("x", ())

    overlay = make_overlay(backend="lexical_lab", chat_url="http://203.0.113.9:8000/v1")
    handler = HandlerMemoryPilot(
        event_bus=bus, failure_recorder=recorder, overlay=overlay, backend=Spy()
    )
    _assert_typed_failure(
        await handler.handle(_req(EnumMemoryPilotOperation.INGEST)),
        bus,
        recorder,
        Code.ENDPOINT_NOT_ALLOWED,
    )
    assert calls == []


async def test_telemetry_left_on_is_an_invalid_overlay(
    bus: RecordingBus,
    recorder: RecordingRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "overlays" / "node_memory_pilot_effect"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "backend": "in_memory",
                "frozen_hash": frozen_hash(),
                "pilot_dir": str(PILOT_DIR),
                "telemetry_disabled": False,
                "allowed_hosts": ["127.0.0.1"],
            }
        )
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    handler = HandlerMemoryPilot(event_bus=bus, failure_recorder=recorder)
    _assert_typed_failure(
        await handler.handle(_req()), bus, recorder, Code.OVERLAY_INVALID
    )


async def test_no_overlay_at_all_is_a_typed_failure_not_a_packaged_default(
    bus: RecordingBus, recorder: RecordingRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    handler = HandlerMemoryPilot(event_bus=bus, failure_recorder=recorder)
    _assert_typed_failure(
        await handler.handle(_req()), bus, recorder, Code.OVERLAY_INVALID
    )


async def test_query_before_ingest_is_not_ingested(
    bus: RecordingBus, recorder: RecordingRecorder
) -> None:
    handler = HandlerMemoryPilot(
        event_bus=bus, failure_recorder=recorder, overlay=make_overlay()
    )
    _assert_typed_failure(
        await handler.handle(_req()), bus, recorder, Code.NOT_INGESTED
    )


async def test_a_backend_crash_is_typed_and_leaks_only_the_exception_type(
    bus: RecordingBus, recorder: RecordingRecorder
) -> None:
    class Boom:
        name = "boom"

        async def ingest(
            self, dataset: str, documents: tuple[ModelMemoryPilotDocument, ...]
        ) -> int:
            raise RuntimeError("secret path /somewhere")

        async def query(self, dataset: str, question: str) -> BackendAnswer:
            raise RuntimeError("secret path /somewhere")

    handler = HandlerMemoryPilot(
        event_bus=bus, failure_recorder=recorder, overlay=make_overlay(), backend=Boom()
    )
    result = await handler.handle(_req(EnumMemoryPilotOperation.INGEST))
    _assert_typed_failure(result, bus, recorder, Code.BACKEND_ERROR)
    assert result.error_detail == "RuntimeError"


async def test_a_dead_bus_does_not_lose_the_result_or_the_failure_row(
    recorder: RecordingRecorder,
) -> None:
    dead = RecordingBus(fail=True)
    handler = HandlerMemoryPilot(
        event_bus=dead, failure_recorder=recorder, overlay=make_overlay()
    )
    result = await handler.handle(_req())
    assert result.error_code is Code.NOT_INGESTED
    assert len(recorder.rows) == 1


def test_a_failed_result_cannot_read_working() -> None:
    with pytest.raises(ValueError, match="reads NOT_WORKING"):
        ModelMemoryPilotResult(
            correlation_id=CORR,
            operation=EnumMemoryPilotOperation.QUERY,
            status=EnumMemoryPilotStatus.FAILED,
            reading=EnumMemoryPilotReading.WORKING,
            backend="x",
            error_code=Code.BACKEND_ERROR,
        )
