# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probe applied sequences using explicit runtime-owned read adapters."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_session_stream_barrier import (
    ModelSessionStreamBarrier,
    ModelSessionStreamBarrierProbe,
)


class ModelSessionBarrierReadBinding(BaseModel):
    """Contract-owned read relations; credentials remain in the runtime env."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dsn_env: str = Field(min_length=1)
    lineage_relation: str = Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
    content_relation: str = Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
    statement_timeout_ms: int = Field(default=1000, gt=0, le=5000)


class PostgresSessionBarrierProbeSource:
    """One read-only snapshot, including holes rather than only MAX(sequence).

    A producer's journal says what was sent, not what either projection applied.
    Only committed projection rows are evidence here. The runtime must explicitly
    supply the connection factory and SQL composer; the orchestrator never loads
    a driver or supplies a default database connection.
    """

    def __init__(
        self,
        binding: ModelSessionBarrierReadBinding,
        *,
        connect: Callable[..., Any] | None = None,
        sql_composer: Any = None,
    ) -> None:
        self._binding = binding
        self._connect = connect
        self._sql = sql_composer

    def read(self, session_id: str, barrier_id: str) -> ModelSessionStreamBarrierProbe:
        # Transport adapters belong to the runtime/effect boundary. An unwired
        # orchestrator falls back instead of discovering a driver or opening its
        # own connection. Explicit adapters keep the projection probe testable.
        if self._connect is None or self._sql is None:
            raise RuntimeError(
                "session barrier requires runtime-injected read adapters"
            )
        sql = self._sql
        dsn = os.environ.get(self._binding.dsn_env)
        if not dsn:
            raise RuntimeError("session barrier projection binding is unavailable")
        lineage = sql.Identifier(*self._binding.lineage_relation.split("."))
        content = sql.Identifier(*self._binding.content_relation.split("."))
        # One statement keeps the two streams and barrier records in one snapshot.
        query = sql.SQL("""
            SELECT 'lineage', producer_seq, NULL::text, NULL::bigint,
                   NULL::text, NULL::timestamptz, lane
              FROM {lineage}
             WHERE session_id = %s AND producer_seq IS NOT NULL
            UNION ALL
            SELECT stream, producer_seq, barrier_id, high_water,
                   content_capture, emitted_at, NULL::text
              FROM {content}
             WHERE session_id = %s
               AND ((stream = 'content' AND producer_seq IS NOT NULL)
                    OR (content_kind = 'barrier' AND barrier_id = %s))
        """).format(lineage=lineage, content=content)
        conn = self._connect(dsn, connect_timeout=3)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, false)",
                    (str(self._binding.statement_timeout_ms),),
                )
                cursor.execute(query, (session_id, session_id, barrier_id))
                rows = cursor.fetchall()
        finally:
            conn.close()
        barriers: dict[str, ModelSessionStreamBarrier] = {}
        applied: dict[str, list[int]] = {"lineage": [], "content": []}
        revisions = []
        lane: str | None = None
        lane_sequence: int | None = None
        for (
            stream,
            sequence,
            row_barrier,
            high_water,
            capture,
            emitted_at,
            row_lane,
        ) in rows:
            if stream not in applied:
                raise ValueError("unknown projected session stream")
            if sequence is not None:
                applied[stream].append(sequence)
                if (
                    stream == "lineage"
                    and row_lane
                    and (lane_sequence is None or sequence > lane_sequence)
                ):
                    lane = row_lane
                    lane_sequence = sequence
            if row_barrier == barrier_id:
                barrier = ModelSessionStreamBarrier(
                    stream=stream,
                    barrier_id=barrier_id,
                    high_water=high_water,
                    content_capture=capture,
                )
                if stream in barriers and barriers[stream] != barrier:
                    raise ValueError("conflicting projected session barriers")
                barriers[stream] = barrier
                revisions.append(emitted_at)
        revision = (
            min(revisions).astimezone(UTC).isoformat().replace("+00:00", "Z")
            if revisions
            else None
        )
        return ModelSessionStreamBarrierProbe(
            session_id=session_id,
            barrier_id=barrier_id,
            barriers=tuple(barriers.values()),
            applied={
                "lineage": tuple(applied["lineage"]),
                "content": tuple(applied["content"]),
            },
            source_revision=revision,
            lane=lane,
        )
