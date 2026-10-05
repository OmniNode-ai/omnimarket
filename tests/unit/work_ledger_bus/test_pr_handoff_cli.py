# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex work-ledger handoff` against the real orchestrator over an in-memory bus (OMN-20636)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from click.testing import CliRunner
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus, event_type_for
from omnimarket.delegated_test_loop.lane_bus import BusKind
from omnimarket.events.topics import PR_HANDOFF_REQUESTED_TOPIC_V1
from omnimarket.models.pr_handoff import ModelPrHandoffRequested
from omnimarket.nodes.node_pr_handoff_orchestrator.event_topics import publish_topic_for
from omnimarket.nodes.node_pr_handoff_orchestrator.handlers import (
    HandlerPrHandoffOrchestrator,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.orchestration.row_store import (
    InMemoryPrHandoffRowStore,
)
from omnimarket.work_ledger_bus import cli
from omnimarket.work_ledger_bus.cli import work_ledger_group
from tests.chains.pr_handoff import _builders as b

pytestmark = pytest.mark.unit


def _with_orchestrator(
    monkeypatch: pytest.MonkeyPatch, *, answer: bool
) -> list[ModelPrHandoffRequested]:
    seen: list[ModelPrHandoffRequested] = []
    orchestrator = HandlerPrHandoffOrchestrator(store=InMemoryPrHandoffRowStore())

    @contextlib.asynccontextmanager
    async def open_bus(
        *,
        bus: BusKind,
        lane: str | None,
        kafka_bootstrap: str | None,
        omni_home: Path | None,
    ) -> AsyncIterator[ProtocolLabRunBus]:
        memory = EventBusInmemory(environment="test", group="pr-handoff-cli")
        await memory.start()

        async def on_request(message: Any) -> None:
            raw = json.loads(message.value)
            request = ModelPrHandoffRequested.model_validate(raw["payload"])
            seen.append(request)
            if not answer:
                return
            for event in await orchestrator.handle(request):
                topic = publish_topic_for(event)
                envelope = ModelEventEnvelope[dict[str, object]](
                    payload=event.model_dump(mode="json"),
                    correlation_id=request.correlation_id,
                    event_type=event_type_for(topic),
                )
                await memory.publish(
                    topic, None, json.dumps(envelope.model_dump(mode="json")).encode()
                )

        await memory.subscribe(
            PR_HANDOFF_REQUESTED_TOPIC_V1, on_message=on_request, group_id="orch"
        )
        try:
            yield memory
        finally:
            await memory.close()

    monkeypatch.setattr(cli, "open_lab_run_bus", open_bus)
    return seen


def _invoke(tmp_path: Path, wait_s: str) -> tuple[int, str, ModelPrHandoffRequested]:
    request = b.request(uuid4())
    path = tmp_path / "request.json"
    path.write_text(request.model_dump_json())
    result = CliRunner().invoke(
        work_ledger_group,
        [
            "handoff",
            "--bus",
            "inmemory",
            "--request-file",
            str(path),
            "--wait-s",
            wait_s,
        ],
    )
    return result.exit_code, result.output, request


def test_the_orchestrators_first_answer_is_printed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _with_orchestrator(monkeypatch, answer=True)
    code, output, request = _invoke(tmp_path, "5")
    assert code == 0, output
    printed = json.loads(output)
    assert printed["topic"] == "onex.evt.omnimarket.pr-handoff-accepted.v1"
    assert printed["answer"]["correlation_id"] == str(request.correlation_id)
    assert [r.correlation_id for r in seen] == [request.correlation_id]


def test_no_answer_in_time_is_pending_and_publish_only_is_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _with_orchestrator(monkeypatch, answer=False)
    code, output, _ = _invoke(tmp_path, "0.2")
    assert code == 75
    assert json.loads(output)["status"] == "pending"
    code, output, _ = _invoke(tmp_path, "0")
    assert code == 0
    assert json.loads(output)["status"] == "published"
