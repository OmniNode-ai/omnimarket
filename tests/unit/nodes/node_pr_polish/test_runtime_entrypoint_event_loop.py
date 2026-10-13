# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A pr-polish-start command reaches the repair-worker delegation on the runtime's loop.

The runtime's auto-wired callback is a coroutine, and it calls the handler's
dispatch entrypoint on the kernel event loop. Before ``handle_async`` the
entrypoint was the synchronous ``handle``: the live workflow ran its blocking
git, gh and pre-commit calls on that loop, and publishing the delegation request
called ``asyncio.run`` inside the running loop. On the dev lane
``KAFKA_BOOTSTRAP_SERVERS`` is set, so the publish was always attempted and the
run ended FAILED with ``asyncio.run() cannot be called from a running event
loop``, never publishing the request.
"""

from __future__ import annotations

import inspect
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _resolve_effective_handle_method,
)

import omnimarket.nodes.node_pr_polish as node_pkg
from omnimarket.nodes.node_pr_polish.handlers.handler_pr_polish import HandlerPrPolish
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_start_command import (
    ModelPrPolishStartCommand,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_state import (
    EnumPrPolishPhase,
)


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.publish_threads: list[int] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: object = None
    ) -> None:
        self.published.append((topic, value))
        self.publish_threads.append(threading.get_ident())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_command_publishes_repair_delegation_from_the_runtime_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = _RecordingBus()
    monkeypatch.setattr(EventBusKafka, "default", lambda *_args: bus)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker.example.test:9092")
    monkeypatch.delenv("ONEX_PR_POLISH_DISABLE_DELEGATION_PUBLISH", raising=False)
    command = ModelPrPolishStartCommand(
        correlation_id=uuid4(),
        repo="OmniNode-ai/omnimarket",
        pr_number=1,
        ticket_id="OMN-21034",
        run_dir=str(tmp_path / "run"),
        requested_at=datetime.now(tz=UTC),
    )

    entrypoint = _resolve_effective_handle_method(HandlerPrPolish())
    assert entrypoint is not None
    loop_thread = threading.get_ident()
    outcome = entrypoint(command)
    completed = await outcome if inspect.isawaitable(outcome) else outcome

    assert completed.error_message is None
    assert completed.final_phase == EnumPrPolishPhase.DONE
    assert completed.delegation_publish_status == "published:1"
    assert completed.repair_workers_dispatched == 1

    contract = yaml.safe_load(
        (Path(node_pkg.__file__).parent / "contract.yaml").read_text()
    )
    declared = set(contract["event_bus"]["publish_topics"])
    assert [topic for topic, _ in bus.published] == [
        t for t in declared if t.endswith("delegation-request.v1")
    ]
    # The blocking workflow ran off the kernel loop's thread.
    assert bus.publish_threads
    assert loop_thread not in bus.publish_threads
