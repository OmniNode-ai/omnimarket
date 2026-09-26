# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full-content session projection: the pure half and the durable half (OMN-19550).

Two classes, per rule 7a as amended by OMN-18769:

* :class:`HandlerProjectionSessionContent` is the pure definition-B fold,
  ``handle(ModelSessionContentRecord) -> ModelSessionContentRow``.
* :class:`SessionContentProjectionWriter` is the entry the runtime's
  projection wiring calls with ``_db`` and ``_topic`` injected. It declares
  in-process dispatch and does nothing but call the fold and persist its row.

Either half built alone fails the same silent way: offsets commit, lag reads
zero, and the table stays empty.

**Retention.** Rows are kept. Nothing in this node deletes or prunes, and the
runtime role is granted no DELETE. Pruning is the archive plan's step, and it
comes only after a verified archive (RULING 2026-09-25T11:20:55Z: nothing is
shortened before its archive is verified by readback). The emitted_at index
is what a window prune would use.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

import yaml

from omnimarket.nodes.node_projection_session_content.handlers.session_content_fold import (
    SUBSCRIBE_TOPIC,
    SessionContentFoldError,
    fold_session_content,
)
from omnimarket.nodes.node_projection_session_content.models.model_session_content import (
    ModelSessionContentRecord,
    ModelSessionContentRow,
    ModelSessionContentWriteResult,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE = "omninode_internal.session_content"

_COLUMNS = (
    "event_id",
    "session_id",
    "turn_id",
    "correlation_id",
    "tool_use_id",
    "tool_name",
    "content_kind",
    "chunk_index",
    "chunk_count",
    "content",
    "command",
    "content_sha256",
    "original_chars",
    "truncated",
    "redaction_state",
    "producer_redaction",
    "hook_source",
    "emitted_at",
    "source_topic",
)
_JSONB = frozenset({"command", "producer_redaction"})

_PLACEHOLDERS = ", ".join(
    f"${i}::jsonb" if name in _JSONB else f"${i}"
    for i, name in enumerate(_COLUMNS, start=1)
)

# The content is immutable for a key (the key content-addresses it), so the
# conflict arm only restamps redaction_state. It exists so a replay reports
# one row to the runtime's write-path guard instead of zero, which that guard
# would log as a failed write.
_UPSERT = f"""
    INSERT INTO {TABLE} ({", ".join(_COLUMNS)})
    VALUES ({_PLACEHOLDERS})
    ON CONFLICT (event_id) DO UPDATE SET
        redaction_state = EXCLUDED.redaction_state
"""

_T = TypeVar("_T")


def _parameters(row: ModelSessionContentRow) -> list[Any]:
    values = row.model_dump()
    return [
        json.dumps(values[name]) if name in _JSONB else values[name]
        for name in _COLUMNS
    ]


class HandlerProjectionSessionContent:
    """The PURE half: one content record in, its row out. No I/O."""

    def handle(self, request: ModelSessionContentRecord) -> ModelSessionContentRow:
        """Canonical definition-B entrypoint."""
        return fold_session_content(request)


class SessionContentProjectionWriter(BaseProjectionRunner):
    """The DURABLE half: the entry the runtime's projection wiring calls."""

    #: Opt in to in-process dispatch (OMN-16874). Undeclared, the runtime
    #: treats a runner-shaped class as standalone and dispatches it nothing,
    #: and no dedicated writer process exists for this node.
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).resolve().parent.parent / "contract.yaml"
        with open(path, encoding="utf-8") as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._dispatch_lock = threading.Lock()

    @property
    def topics(self) -> list[str]:
        declared = list(self._contract.get("event_bus", {}).get("subscribe_topics", []))
        if declared != [SUBSCRIBE_TOPIC]:
            raise SessionContentFoldError(
                f"contract subscribe_topics {declared} disagree with the fold's "
                f"{[SUBSCRIBE_TOPIC]}"
            )
        return declared

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """One injected message, one write, and the count the runtime reads.

        ``rows_upserted`` is the key the runtime's write-path guard reads; any
        other shape is treated as zero rows and suppresses the terminal event.
        """
        topic = str(input_data.pop("_topic", SUBSCRIBE_TOPIC))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        for injected in [k for k in input_data if k.startswith("_")]:
            input_data.pop(injected)
        with self._dispatch_lock:
            result = self._run(self._project_one_message(topic, input_data, meta))
        return result.model_dump()

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> ModelSessionContentWriteResult:
        """Own the pool for exactly the loop this message is projected on."""
        try:
            await self.db.connect()
            return await self._write(topic, data)
        finally:
            await self.db.close()

    async def _write(
        self, topic: str, data: dict[str, Any]
    ) -> ModelSessionContentWriteResult:
        if topic != SUBSCRIBE_TOPIC:
            raise SessionContentFoldError(
                f"topic {topic!r} is not subscribed by node_projection_session_content"
            )
        row = fold_session_content(
            ModelSessionContentRecord.model_validate(data), source_topic=topic
        )
        await self.db.execute(_UPSERT, *_parameters(row))
        return ModelSessionContentWriteResult(rows_upserted=1, event_id=row.event_id)

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """The base runner's standalone entry. Same write as :meth:`handle`."""
        await self._write(topic, data)
        return True

    @staticmethod
    def _run(coro: Coroutine[Any, Any, _T]) -> _T:
        """Drive one coroutine from a synchronous entry, inside a loop or not."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()


__all__ = [
    "TABLE",
    "HandlerProjectionSessionContent",
    "SessionContentProjectionWriter",
]
