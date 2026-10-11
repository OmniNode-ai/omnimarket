# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus host of the operator capture: operator prompts off the bus (OMN-20905).

RULING 2026-10-10T22:40:43Z: no capture hooks; the operator's decisions and asks are extracted
asynchronously from the bus. The host runs inside the work-ledger serve process on the ledger
host when that process is started with ``--operator-capture``: the ledger is a file there, the
serve process already holds the bus and the ledger's own append runner, and no second daemon is
installed. It reads the content-capture topic in one consumer group, so one host takes each
prompt, from offset ``latest`` when the group is new: the capture covers prompts said after it
starts, never a replay of the topic's history.

Nearly every record on that topic is tool input, tool output or an assistant reply, and none of
those is read: a record whose bytes do not name the operator-prompt content kind is skipped
before it is parsed. A lane brief, a scheduled prompt or a task notification never carries that
kind (the producer marks only prompts whose transcript origin is human), so a lane yields no row.

Each operator prompt is handled one at a time, off the event loop, and answered with one receipt
on the node's terminal topic. A prompt whose rows could not all be appended stays in the store's
inbox; the host retries the inbox every ``retry_seconds``.

Topics come only from the node's ``contract.yaml``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from importlib import resources
from pathlib import Path
from typing import Protocol

import yaml
from omnibase_core.event_bus.util_consumer_group import derive_service_group_id
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.lab_work.bus import _bytes, _subscribe
from omnimarket.models.operator_capture import (
    OPERATOR_PROMPT_KIND,
    ModelCaptureProcessResult,
    ModelOperatorCaptureReceipt,
    ModelOperatorCaptureTopics,
    ModelOperatorPromptRecord,
)
from omnimarket.nodes.node_operator_capture_effect.handlers import capture_store
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    onex_delegate_runner,
    runner_ledger_appender,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_operator_capture_effect import (
    HandlerOperatorCaptureEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ProtocolLedgerAppendRunner,
)
from omnimarket.projection.envelope import unwrap_envelope

logger = logging.getLogger(__name__)
NODE = "node_operator_capture_effect"
GROUP_SERVICE = "omnimarket"
DEFAULT_RETRY_SECONDS = 300.0
_KIND_NEEDLE = OPERATOR_PROMPT_KIND.encode("utf-8")


class ProtocolOperatorCaptureHandler(Protocol):
    @property
    def host_name(self) -> str: ...

    def handle(
        self, record: ModelOperatorPromptRecord
    ) -> ModelOperatorCaptureReceipt: ...

    def retry(self) -> ModelCaptureProcessResult: ...


def load_operator_capture_topics() -> ModelOperatorCaptureTopics:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    dispatch = yaml.safe_load(text)["runtime_dispatch"]
    return ModelOperatorCaptureTopics(
        prompts=dispatch["subscribe_topic"],
        receipt=dispatch["terminal_events"]["success"],
    )


def capture_group_id() -> str:
    """One group for every serve process: one host takes each prompt, so no row is doubled."""
    return derive_service_group_id(NODE, service=GROUP_SERVICE)


def operator_prompt_of(value: bytes) -> ModelOperatorPromptRecord | None:
    """The record when this message is the first chunk of an operator prompt, else None."""
    if _KIND_NEEDLE not in value:
        return None
    payload = unwrap_envelope(value)
    if payload is None:
        return None
    try:
        record = ModelOperatorPromptRecord.model_validate(payload)
    except ValidationError:
        return None
    if not record.is_operator_prompt or record.chunk_index != 0:
        return None
    if not record.content.strip():
        return None
    return record


class OperatorCaptureHost:
    """Serve operator prompts from the content-capture topic on the ledger host."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        handler: ProtocolOperatorCaptureHandler,
        *,
        topics: ModelOperatorCaptureTopics | None = None,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
    ) -> None:
        self._bus = bus
        self._handler = handler
        self._topics = topics or load_operator_capture_topics()
        self._retry_seconds = retry_seconds
        self._lock = asyncio.Lock()
        self._unsubscribe: Callable[[], Awaitable[None]] | None = None
        self._retry_task: asyncio.Task[None] | None = None
        self.skipped = 0
        self.handled = 0

    @property
    def topics(self) -> ModelOperatorCaptureTopics:
        return self._topics

    async def start(self) -> None:
        if self._unsubscribe is not None:
            return
        self._unsubscribe = await _subscribe(
            self._bus,
            self._topics.prompts,
            self._on_message,
            capture_group_id(),
            "latest",
        )
        if self._retry_seconds > 0:
            self._retry_task = asyncio.create_task(self._retry_loop())

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            await self._unsubscribe()
            self._unsubscribe = None
        if self._retry_task is not None:
            self._retry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._retry_task
            self._retry_task = None

    async def _on_message(self, message: ProtocolBusMessage) -> None:
        record = operator_prompt_of(message.value)
        if record is None:
            self.skipped += 1
            return
        # A failure to take the prompt into the store raises out of the callback, so the
        # bus redelivers it instead of advancing past it.
        async with self._lock:
            receipt = await asyncio.to_thread(self._handler.handle, record)
        self.handled += 1
        await self._publish(receipt)

    async def _retry_loop(self) -> None:
        while True:
            await asyncio.sleep(self._retry_seconds)
            try:
                async with self._lock:
                    result = await asyncio.to_thread(self._handler.retry)
            except (OSError, RuntimeError, ValueError):
                logger.exception("operator-capture: inbox retry raised")
                continue
            if result.processed or result.errors:
                logger.info(
                    "operator-capture retry processed=%d rows=%d pending=%d errors=%s",
                    result.processed,
                    result.rows_appended,
                    result.pending,
                    list(result.errors),
                )

    async def _publish(self, receipt: ModelOperatorCaptureReceipt) -> None:
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=receipt.model_dump(mode="json"),
            event_type=event_type_for(self._topics.receipt),
        )
        await self._bus.publish(
            self._topics.receipt,
            receipt.session_id.encode("utf-8"),
            _bytes(envelope),
        )
        logger.info(
            "operator-capture host %s session %s %s rows=%d pending=%d errors=%s",
            receipt.host,
            receipt.session_id,
            receipt.status,
            receipt.result.rows_appended,
            receipt.result.pending,
            list(receipt.result.errors),
        )


def ledger_host_capture(
    bus: ProtocolLabRunBus,
    *,
    ledger: Path,
    append_runner: ProtocolLedgerAppendRunner,
    host_name: str,
    bus_lane: str | None,
    retry_seconds: float = DEFAULT_RETRY_SECONDS,
) -> OperatorCaptureHost:
    """The capture host the work-ledger serve process runs beside its own.

    It classifies through ``onex delegate`` on the serve process's own bus lane and appends
    through the serve process's own append runner, so every row passes the ledger's lock and
    guards and is never re-published to the bus.
    """
    handler = HandlerOperatorCaptureEffect(
        store_dir=capture_store.store_dir(),
        ledger_path=ledger,
        host_name=host_name,
        delegate=onex_delegate_runner(bus_lane),
        append=runner_ledger_appender(append_runner),
    )
    return OperatorCaptureHost(bus, handler, retry_seconds=retry_seconds)


__all__ = [
    "DEFAULT_RETRY_SECONDS",
    "OperatorCaptureHost",
    "ProtocolOperatorCaptureHandler",
    "capture_group_id",
    "ledger_host_capture",
    "load_operator_capture_topics",
    "operator_prompt_of",
]
