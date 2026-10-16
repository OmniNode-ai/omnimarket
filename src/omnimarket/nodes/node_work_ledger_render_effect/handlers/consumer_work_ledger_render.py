# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus side of the markdown projection: subscribe, parse, step the handler, publish (OMN-20275).

Every topic comes from the node contract. A payload that does not parse as a row event
(not JSON, no ``ledger_seq``, a ``row_id`` that is not the hash of its text) goes to the
malformed topic and is acknowledged, so it never holds an offset back. The offsets the
handler reports as safe are kept in ``committed_offsets`` for the runtime's consumer to
commit; this class commits nothing itself.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import ValidationError

from omnimarket.models.model_work_ledger_render_row import ModelWorkLedgerRenderRow
from omnimarket.nodes.node_work_ledger_render_effect.contract_topics import (
    ALL_SUBSCRIBE_TOPICS,
    TOPIC_BLOCKED,
    TOPIC_MALFORMED,
    TOPIC_RENDERED,
)
from omnimarket.nodes.node_work_ledger_render_effect.handlers.handler_work_ledger_render import (
    HandlerWorkLedgerRender,
)
from omnimarket.nodes.node_work_ledger_render_effect.models.model_work_ledger_render import (
    EnumWorkLedgerRenderKind,
    ModelWorkLedgerRenderRequest,
    ModelWorkLedgerRenderResult,
)


class _Bus(Protocol):
    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[Any], Awaitable[None]],
        group_id: str,
    ) -> Callable[[], Awaitable[None]]: ...

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> Any: ...


_GROUP = "work-ledger-render-effect"


class ConsumerWorkLedgerRender:
    def __init__(self, handler: HandlerWorkLedgerRender, bus: _Bus) -> None:
        self._handler = handler
        self._bus = bus
        self._unsubscribe: list[Callable[[], Awaitable[None]]] = []
        self.committed_offsets: dict[str, int] = {}

    async def start(self) -> None:
        for topic in ALL_SUBSCRIBE_TOPICS:
            self._unsubscribe.append(
                await self._bus.subscribe(
                    topic, on_message=self._on_message, group_id=_GROUP
                )
            )

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribe:
            await unsubscribe()
        self._unsubscribe.clear()

    async def probe(self) -> ModelWorkLedgerRenderResult:
        """The lag probe, run on a timer by the host: read the ledger of record's head."""
        result = self._handler.handle(
            ModelWorkLedgerRenderRequest(kind=EnumWorkLedgerRenderKind.PROBE)
        )
        await self._report(result)
        return result

    async def _on_message(self, message: Any) -> None:
        topic = str(message.topic)
        offset = int(message.offset)
        try:
            body = json.loads(message.value)
            row = ModelWorkLedgerRenderRow.model_validate(body)
        except (ValueError, ValidationError, TypeError) as exc:
            await self._bus.publish(
                TOPIC_MALFORMED,
                None,
                json.dumps(
                    {"topic": topic, "offset": offset, "error": str(exc)[:500]}
                ).encode(),
            )
            self._record(self._handler.acknowledge(topic, offset))
            return
        result = self._handler.handle(
            ModelWorkLedgerRenderRequest(
                kind=EnumWorkLedgerRenderKind.ROW, row=row, topic=topic, offset=offset
            )
        )
        await self._report(result)

    def _record(self, commit: dict[str, int]) -> None:
        self.committed_offsets.update(commit)

    async def _report(self, result: ModelWorkLedgerRenderResult) -> None:
        self._record(result.commit_offsets)
        if result.rendered_seqs:
            await self._bus.publish(
                TOPIC_RENDERED,
                result.ledger_id.encode(),
                result.model_dump_json(exclude={"blocked"}).encode(),
            )
        if result.blocked is not None:
            await self._bus.publish(
                TOPIC_BLOCKED,
                result.ledger_id.encode(),
                result.model_dump_json().encode(),
            )


__all__: list[str] = ["ConsumerWorkLedgerRender"]
