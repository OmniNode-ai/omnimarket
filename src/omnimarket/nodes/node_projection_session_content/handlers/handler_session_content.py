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

import json
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.writer_pool_host import (
    WriterPoolHost,
    build_pool_adapter,
    load_writer_pool_settings,
)
from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.models.model_writer_pool_settings import ModelWriterPoolSettings
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
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
)

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
    "content_record_id",
    "stream",
    "producer_seq",
    "record_type",
    "barrier_id",
    "high_water",
    "content_capture",
)
_JSONB = frozenset({"command", "producer_redaction"})

_PLACEHOLDERS = ", ".join(
    f"${i}::jsonb" if name in _JSONB else f"${i}"
    for i, name in enumerate(_COLUMNS, start=1)
)

# The content is immutable for a key (the key content-addresses it), so the
# conflict arm restamps redaction_state and fills producer stamps that the
# pre-OMN-21334 fold discarded. Populated stamps never change on replay. A
# replay reports one row to the runtime's write-path guard rather than zero.
_UPSERT = f"""
    INSERT INTO {TABLE} AS existing ({", ".join(_COLUMNS)})
    VALUES ({_PLACEHOLDERS})
    ON CONFLICT (event_id) DO UPDATE SET
        redaction_state = EXCLUDED.redaction_state,
        content_record_id = COALESCE(existing.content_record_id, EXCLUDED.content_record_id),
        stream = COALESCE(existing.stream, EXCLUDED.stream),
        producer_seq = COALESCE(existing.producer_seq, EXCLUDED.producer_seq),
        record_type = COALESCE(existing.record_type, EXCLUDED.record_type),
        barrier_id = COALESCE(existing.barrier_id, EXCLUDED.barrier_id),
        high_water = COALESCE(existing.high_water, EXCLUDED.high_water),
        content_capture = COALESCE(existing.content_capture, EXCLUDED.content_capture)
"""


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

    def __init__(
        self,
        contract_path: Path | None = None,
        pool_settings: ModelWriterPoolSettings | None = None,
    ) -> None:
        super().__init__()
        path = contract_path or Path(__file__).resolve().parent.parent / "contract.yaml"
        with open(path, encoding="utf-8") as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        # The pool is sized by the contract's writer_pool, and opened by the
        # first message because the runtime binds the DSN after construction.
        self._pool_host = WriterPoolHost(
            "session_content_writer",
            lambda: build_pool_adapter(self.db, self._pool_host.settings),
            pool_settings
            or load_writer_pool_settings("node_projection_session_content"),
        )

    def start_pool(self) -> None:
        """Open the one pool now rather than on the first message."""
        self._pool_host.start()

    def close_pool(self) -> None:
        """Close the pool; a message after this raises ``WriterPoolClosedError``."""
        self._pool_host.close()

    @property
    def topics(self) -> list[str]:
        declared = list(self._contract.get("event_bus", {}).get("subscribe_topics", []))
        if declared != [SUBSCRIBE_TOPIC]:
            raise SessionContentFoldError(
                f"contract subscribe_topics {declared} disagree with the fold's "
                f"{[SUBSCRIBE_TOPIC]}"
            )
        return declared

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        """One injected message, one write, and the count the runtime reads.

        ``rows_upserted`` is the key the runtime's write-path guard reads; any
        other shape is treated as zero rows and suppresses the terminal event.
        """
        topic = request.topic_or(SUBSCRIBE_TOPIC)
        data = {k: v for k, v in request.payload.items() if not k.startswith("_")}
        result = self._pool_host.run(lambda db: self._write(db, topic, data))
        return result.model_dump()

    async def _write(
        self, db: Any, topic: str, data: dict[str, Any]
    ) -> ModelSessionContentWriteResult:
        if topic != SUBSCRIBE_TOPIC:
            raise SessionContentFoldError(
                f"topic {topic!r} is not subscribed by node_projection_session_content"
            )
        row = fold_session_content(
            ModelSessionContentRecord.model_validate(data), source_topic=topic
        )
        await db.execute(_UPSERT, *_parameters(row))
        return ModelSessionContentWriteResult(rows_upserted=1, event_id=row.event_id)

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """The base runner's standalone entry. Same write as :meth:`handle`."""
        await self._write(self.db, topic, data)
        return True


__all__ = [
    "TABLE",
    "HandlerProjectionSessionContent",
    "SessionContentProjectionWriter",
]
