# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection writer for the lab job read models (OMN-20604).

The effect-class half of the rule-7a pair and the entry the runtime calls. It
holds no business logic beyond calling the fold and persisting its result; the
derivation is imported from ``HandlerProjectionLabJob``, so the writer and
the fold cannot disagree about what a row means. It is the only writer of both
tables (column ownership, plan section 8).

Two ways to get this shape wrong produce the SAME silent symptom -- every
message consumed, offsets committed, zero rows, no error. A pure entry on the
projection arm validates and returns without writing. A runner-shaped class
that does not declare in-process dispatch is skipped by the shared runtime.
Both are pinned by tests.

The runtime reads ``rows_upserted`` and ``rows_refused_by_ordering_guard``
from the returned applied-event payload. Each message owns its adapter and
loop, so concurrent dispatches cannot share or close another message's pool.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from omnimarket.events.topics import LAB_JOB_TRANSITIONED_TOPIC_V1
from omnimarket.nodes.node_projection_lab_job.handlers.handler_projection_lab_job import (
    HandlerProjectionLabJob,
)
from omnimarket.nodes.node_projection_lab_job.models import (
    EnumLabJobProjectionEventKind,
    ModelLabJobProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

if TYPE_CHECKING:
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

TABLE_LAB_JOB_STATE = "omninode_internal.lab_job_state"
TABLE_LAB_JOB_TRANSITIONS = "omninode_internal.lab_job_transitions"

#: The key the runtime's write-path guard and apply counters read the row count
#: from (omnibase_infra ``_extract_rows_upserted``). Any other key reads as 0.
ROWS_UPSERTED_KEY = "rows_upserted"

#: The key the runtime reads for statements an ordering guard declined
#: (omnibase_infra ``ROWS_REFUSED_KEY``, OMN-18992). Restated rather than
#: imported: this module is loaded by processes that do not import the
#: runtime's wiring package.
ROWS_REFUSED_KEY = "rows_refused_by_ordering_guard"

#: Which request field each subscribed topic fills.
TOPIC_EVENT_KIND: Mapping[str, EnumLabJobProjectionEventKind] = {
    LAB_JOB_TRANSITIONED_TOPIC_V1: EnumLabJobProjectionEventKind.TRANSITIONED,
}

# Append-only. A redelivery inserts nothing and returns no row, so it is not
# counted as written. The table grant withholds UPDATE as well.
_APPEND_TRANSITION = f"""
    INSERT INTO {TABLE_LAB_JOB_TRANSITIONS} (
        job_id, seq, from_state, to_state, attempt, at, reason, recorded_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
    ON CONFLICT (job_id, seq) DO NOTHING
    RETURNING job_id, seq, to_state
"""

# Keyed job_id; the CAS-assigned seq is the only ordering authority. Equal and
# lower seqs are refused in the conflict arm's WHERE, never a read-then-write,
# which two consumers would race with both writes succeeding.
_UPSERT_STATE = f"""
    INSERT INTO {TABLE_LAB_JOB_STATE} (
        job_id, kind, state, episode, attempt, seq, entered_state_at,
        claimed_at, next_dispatch_at, owner_runtime, work_unit_id, run_id,
        last_verdict, last_outcome, time_box_hit, continuation, failure_reason,
        parent_lane, ticket, alert_sent_at, spec, first_seen_at, updated_at
    )
    VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
        $13, $14, $15, $16, $17, $18, $19, $20, $21::jsonb, NOW(), NOW()
    )
    ON CONFLICT (job_id) DO UPDATE SET
        kind = EXCLUDED.kind,
        state = EXCLUDED.state,
        episode = EXCLUDED.episode,
        attempt = EXCLUDED.attempt,
        seq = EXCLUDED.seq,
        entered_state_at = EXCLUDED.entered_state_at,
        claimed_at = EXCLUDED.claimed_at,
        next_dispatch_at = EXCLUDED.next_dispatch_at,
        owner_runtime = EXCLUDED.owner_runtime,
        work_unit_id = EXCLUDED.work_unit_id,
        run_id = EXCLUDED.run_id,
        last_verdict = EXCLUDED.last_verdict,
        last_outcome = EXCLUDED.last_outcome,
        time_box_hit = EXCLUDED.time_box_hit,
        continuation = EXCLUDED.continuation,
        failure_reason = EXCLUDED.failure_reason,
        parent_lane = EXCLUDED.parent_lane,
        ticket = EXCLUDED.ticket,
        alert_sent_at = EXCLUDED.alert_sent_at,
        spec = EXCLUDED.spec,
        updated_at = NOW()
    WHERE EXCLUDED.seq > {TABLE_LAB_JOB_STATE}.seq
    RETURNING job_id, seq, state, episode, projection_cursor
"""


def _state_params(row: dict[str, Any]) -> tuple[Any, ...]:
    # model_dump(mode="python") retains timestamps and enum values. The spec
    # alone crosses the SQL seam as canonical JSON, or SQL NULL for adoption.
    spec = row["spec"]
    return (
        row["job_id"],
        row["kind"],
        row["state"],
        row["episode"],
        row["attempt"],
        row["seq"],
        row["entered_state_at"],
        row["claimed_at"],
        row["next_dispatch_at"],
        row["owner_runtime"],
        row["work_unit_id"],
        row["run_id"],
        row["last_verdict"],
        row["last_outcome"],
        row["time_box_hit"],
        row["continuation"],
        row["failure_reason"],
        row["parent_lane"],
        row["ticket"],
        row["alert_sent_at"],
        json.dumps(spec, sort_keys=True, separators=(",", ":"))
        if spec is not None
        else None,
    )


def _transition_params(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["job_id"],
        row["seq"],
        row["from_state"],
        row["to_state"],
        row["attempt"],
        row["at"],
        row["reason"],
    )


class LabJobProjectionWriter(BaseProjectionRunner):
    """Projects each lab job transition into the two lab job read models.

    Named ``Writer`` rather than ``Runner``: the OMN-14350 type-word ratchet
    hard-fails ``Runner`` in a class name.
    """

    #: Dispatched IN-PROCESS by the runtime auto-wiring, once per consumed
    #: message. ``handle()`` opens one event loop per message, so every
    #: loop-bound resource is opened and closed inside it. Without this
    #: declaration the shared runtime treats the class as a standalone runner
    #: and never dispatches it: there is no dedicated writer Deployment for this
    #: node, so undeclared means zero rows, no error, every watermark healthy.
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._derive = HandlerProjectionLabJob()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim: one message, one loop, one pool."""
        data = dict(input_data)
        topic = str(data.pop("_topic", ""))
        meta = MessageMeta(
            partition=int(data.pop("_partition", 0)),
            offset=int(data.pop("_offset", 0)),
            fallback_id=str(data.pop("_fallback_id", "")),
            topic=topic,
        )
        return asyncio.run(self._project_one_message(topic, data, meta))

    def _adapter_for_one_message(self) -> AsyncpgAdapter:
        """A database adapter owned by one message and the loop that serves it.

        Never the shared ``self.db``: the runtime routes this one instance on
        its topic and calls ``handle()`` from worker threads, so a pool
        on the shared adapter would be created on one thread's loop and used or
        closed from another's (``PoolConnectionHolder.wait_until_released`` on
        the dev lane). The DSN is the one the runtime bound on ``self.db``.
        """
        from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

        return AsyncpgAdapter(dsn=self.db.dsn, min_size=1, max_size=2)

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        """Project one message and report what it wrote, as a row COUNT.

        The runtime's write-path guard reads ``rows_upserted``. A truthy
        acknowledgement over a message that wrote nothing would be
        indistinguishable from one that wrote rows.
        """
        db = self._adapter_for_one_message()
        await db.connect()
        try:
            return await self._project_event(topic, data, db)
        finally:
            await db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone-runner entrypoint: project one message, report success."""
        await self._project_event(topic, data, self.db)
        return True

    @staticmethod
    def build_request(
        topic: str, data: Mapping[str, Any]
    ) -> ModelLabJobProjectionRequest:
        """Map a bare runtime event dict on ``topic`` to the fold's request.

        The runtime injects underscore-prefixed keys beside the payload. The
        frozen event classes forbid extra fields, so those keys are dropped
        here; every other key is the event's own and is validated strictly.
        """
        kind = TOPIC_EVENT_KIND.get(topic)
        if kind is None:
            msg = f"node_projection_lab_job does not consume topic {topic!r}"
            raise ValueError(msg)
        payload = {key: value for key, value in data.items() if not key.startswith("_")}
        return ModelLabJobProjectionRequest.model_validate({kind.value: payload})

    async def _project_event(
        self, topic: str, data: dict[str, Any], db: AsyncpgAdapter
    ) -> dict[str, Any]:
        request = self.build_request(topic, data)
        result = self._derive.handle(request)

        appended = await db.execute(
            _APPEND_TRANSITION, *_transition_params(result.transition_row)
        )
        transition_rows = [dict(row) for row in appended]
        upserted = await db.execute(_UPSERT_STATE, *_state_params(result.state_row))
        state_rows = [dict(row) for row in upserted]

        written = len(transition_rows) + len(state_rows)
        return {
            "event_kind": request.event_kind.value,
            "job_id": result.state_row["job_id"],
            "seq": result.state_row["seq"],
            ROWS_UPSERTED_KEY: written,
            # Refused by the stale-write guard: an older or equal seq, or a
            # redelivered transition. Deliberately not counted as written.
            ROWS_REFUSED_KEY: 2 - written,
            "state_rows": state_rows,
            "transition_rows": transition_rows,
            "state_write_refused": not state_rows,
        }


__all__ = [
    "ROWS_REFUSED_KEY",
    "ROWS_UPSERTED_KEY",
    "TABLE_LAB_JOB_STATE",
    "TABLE_LAB_JOB_TRANSITIONS",
    "TOPIC_EVENT_KIND",
    "LabJobProjectionWriter",
]
