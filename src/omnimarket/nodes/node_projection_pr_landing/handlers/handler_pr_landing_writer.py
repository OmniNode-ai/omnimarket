# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection writer for the PR landing read models (OMN-19833).

The effect-class half of the rule-7a pair and the entry the runtime calls. It
holds no business logic beyond calling the fold and persisting its result; the
derivation is imported from ``HandlerProjectionPrLanding``, so the writer and
the fold cannot disagree about what a row means. It is the only writer of both
tables (column ownership, plan section 8).

Two ways to get this shape wrong produce the SAME silent symptom -- every
message consumed, offsets committed, zero rows, no error. A pure entry on the
projection arm validates and returns without writing. A runner-shaped class
that does not declare in-process dispatch is skipped by the shared runtime.
Both are pinned by tests.

Three more ways it went wrong on the runtime, all fixed here and pinned
(OMN-19833, measured on the .201 dev lane 2026-09-30T03:27Z to 03:39Z):

* The runtime counts a write only under ``rows_upserted``. A count under any
  other key reads as zero, so a writer that did write tripped the
  ``projection_apply_divergence`` health dimension and turned the runtime
  DEGRADED.
* One writer instance is routed on all four topics, and the runtime runs
  handlers in worker threads, so two topics' messages can be in ``handle()`` at
  once. A pool held on the shared adapter was then opened on one thread's loop
  and used or closed from the other's. Each message opens its own adapter.
* A redelivery the ordering guard refuses is reported under the runtime's
  refusal key, so it is logged as a deliberate refusal and not as a writer that
  silently wrote nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from omnimarket.events.topics import (
    PR_LANDING_AGENT_NEEDED_TOPIC_V1,
    PR_LANDING_CLOSED_TOPIC_V1,
    PR_LANDING_MERGED_TOPIC_V1,
    PR_LANDING_TRANSITIONED_TOPIC_V1,
)
from omnimarket.nodes.node_projection_pr_landing.handlers.handler_projection_pr_landing import (
    HandlerProjectionPrLanding,
)
from omnimarket.nodes.node_projection_pr_landing.models import (
    EnumPrLandingProjectionEventKind,
    ModelPrLandingProjectionRequest,
    ModelPrLandingStateRow,
    ModelPrLandingTransitionRow,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

if TYPE_CHECKING:
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

TABLE_PR_LANDING_STATE = "omninode_internal.pr_landing_state"
TABLE_PR_LANDING_TRANSITIONS = "omninode_internal.pr_landing_transitions"

#: The key the runtime's write-path guard and apply counters read the row count
#: from (omnibase_infra ``_extract_rows_upserted``). Any other key reads as 0.
ROWS_UPSERTED_KEY = "rows_upserted"

#: The key the runtime reads for statements an ordering guard declined
#: (omnibase_infra ``ROWS_REFUSED_KEY``, OMN-18992). Restated rather than
#: imported: this module is loaded by processes that do not import the
#: runtime's wiring package.
ROWS_REFUSED_KEY = "rows_refused_by_ordering_guard"

#: Which request field each subscribed topic fills.
TOPIC_EVENT_KIND: Mapping[str, EnumPrLandingProjectionEventKind] = {
    PR_LANDING_TRANSITIONED_TOPIC_V1: EnumPrLandingProjectionEventKind.TRANSITIONED,
    PR_LANDING_AGENT_NEEDED_TOPIC_V1: EnumPrLandingProjectionEventKind.AGENT_NEEDED,
    PR_LANDING_MERGED_TOPIC_V1: EnumPrLandingProjectionEventKind.MERGED,
    PR_LANDING_CLOSED_TOPIC_V1: EnumPrLandingProjectionEventKind.CLOSED,
}

# Append-only. A redelivered transition inserts nothing and returns no row, so
# it is not counted as written. The table grant withholds UPDATE as well.
_APPEND_TRANSITION = f"""
    INSERT INTO {TABLE_PR_LANDING_TRANSITIONS} (
        repository, pr_number, seq, head_sha, from_state, to_state, trigger,
        intents, opens_episode, transitioned_at, first_seen_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, NOW())
    ON CONFLICT (repository, pr_number, seq) DO NOTHING
    RETURNING repository, pr_number, seq, to_state, projection_cursor
"""

# Keyed (repository, pr_number); seq is the ordering authority. The stale-write
# guard is the conflict arm's WHERE, never a read-then-write, which two
# consumers would race with both writes succeeding.
#
# A lower seq is refused. An equal seq is accepted only when it adds the half
# of the transition the row has not seen: the transition itself (the row holds
# no trigger at this seq yet), or its agent-needed or terminal event, which
# re-assert values only they carry. So the two events of one transition merge
# to the same row in either order, and a redelivered transition is refused.
#
# $7 is the episode a terminal asserts (NULL otherwise); $8 marks a reopen,
# which adds one to the stored episode, only when this write moves seq forward
# so that a merge of the other half at the same seq cannot count it twice.
_UPSERT_STATE = f"""
    INSERT INTO {TABLE_PR_LANDING_STATE} (
        repository, pr_number, seq, state, head_sha, episode, last_trigger,
        agent_reason, agent_detail, terminal_at, event_at,
        first_seen_at, updated_at
    )
    VALUES (
        $1, $2, $3, $4, $5,
        COALESCE($7::integer, CASE WHEN $8::boolean THEN 1 ELSE 0 END),
        $6, $9, $10, $11, $12, NOW(), NOW()
    )
    ON CONFLICT (repository, pr_number) DO UPDATE SET
        state = EXCLUDED.state,
        head_sha = COALESCE(EXCLUDED.head_sha, {TABLE_PR_LANDING_STATE}.head_sha),
        episode = CASE
            WHEN $7::integer IS NOT NULL THEN $7::integer
            WHEN $8::boolean AND {TABLE_PR_LANDING_STATE}.seq < EXCLUDED.seq
                THEN {TABLE_PR_LANDING_STATE}.episode + 1
            ELSE {TABLE_PR_LANDING_STATE}.episode
        END,
        last_trigger = CASE
            WHEN EXCLUDED.last_trigger IS NOT NULL THEN EXCLUDED.last_trigger
            WHEN {TABLE_PR_LANDING_STATE}.seq = EXCLUDED.seq
                THEN {TABLE_PR_LANDING_STATE}.last_trigger
            ELSE NULL
        END,
        agent_reason = CASE
            WHEN EXCLUDED.agent_reason IS NOT NULL THEN EXCLUDED.agent_reason
            WHEN EXCLUDED.state = 'NEEDS_AGENT'
                THEN {TABLE_PR_LANDING_STATE}.agent_reason
            ELSE NULL
        END,
        agent_detail = CASE
            WHEN EXCLUDED.agent_reason IS NOT NULL THEN EXCLUDED.agent_detail
            WHEN EXCLUDED.state = 'NEEDS_AGENT'
                THEN {TABLE_PR_LANDING_STATE}.agent_detail
            ELSE NULL
        END,
        terminal_at = CASE
            WHEN EXCLUDED.terminal_at IS NOT NULL THEN EXCLUDED.terminal_at
            WHEN EXCLUDED.state IN ('MERGED', 'CLOSED')
                THEN {TABLE_PR_LANDING_STATE}.terminal_at
            ELSE NULL
        END,
        event_at = CASE
            WHEN {TABLE_PR_LANDING_STATE}.seq = EXCLUDED.seq
                THEN GREATEST({TABLE_PR_LANDING_STATE}.event_at, EXCLUDED.event_at)
            ELSE EXCLUDED.event_at
        END,
        seq = EXCLUDED.seq,
        updated_at = NOW()
    WHERE {TABLE_PR_LANDING_STATE}.seq < EXCLUDED.seq
       OR (
            {TABLE_PR_LANDING_STATE}.seq = EXCLUDED.seq
            AND (
                EXCLUDED.last_trigger IS NULL
                OR {TABLE_PR_LANDING_STATE}.last_trigger IS NULL
            )
       )
    RETURNING repository, pr_number, seq, state, episode, projection_cursor
"""


def _state_params(row: ModelPrLandingStateRow) -> tuple[Any, ...]:
    return (
        row.repository,
        row.pr_number,
        row.seq,
        row.state.value,
        row.head_sha,
        row.trigger,
        row.episode,
        row.opens_episode,
        row.agent_reason.value if row.agent_reason is not None else None,
        row.agent_detail,
        row.terminal_at,
        row.event_at,
    )


def _transition_params(row: ModelPrLandingTransitionRow) -> tuple[Any, ...]:
    return (
        row.repository,
        row.pr_number,
        row.seq,
        row.head_sha,
        row.from_state.value if row.from_state is not None else None,
        row.to_state.value,
        row.trigger,
        row.intents_json(),
        row.opens_episode,
        row.transitioned_at,
    )


class PrLandingProjectionWriter(BaseProjectionRunner):
    """Projects the four landing events into the two landing read models.

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
        self._derive = HandlerProjectionPrLanding()

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
        all four topics and calls ``handle()`` from worker threads, so a pool
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
    ) -> ModelPrLandingProjectionRequest:
        """Map a bare runtime event dict on ``topic`` to the fold's request.

        The runtime injects underscore-prefixed keys beside the payload. The
        frozen event classes forbid extra fields, so those keys are dropped
        here; every other key is the event's own and is validated strictly.
        """
        kind = TOPIC_EVENT_KIND.get(topic)
        if kind is None:
            msg = f"node_projection_pr_landing does not consume topic {topic!r}"
            raise ValueError(msg)
        payload = {key: value for key, value in data.items() if not key.startswith("_")}
        return ModelPrLandingProjectionRequest.model_validate({kind.value: payload})

    async def _project_event(
        self, topic: str, data: dict[str, Any], db: AsyncpgAdapter
    ) -> dict[str, Any]:
        request = self.build_request(topic, data)
        result = self._derive.handle(request)

        transition_rows: list[dict[str, Any]] = []
        if result.transition_row is not None:
            appended = await db.execute(
                _APPEND_TRANSITION, *_transition_params(result.transition_row)
            )
            transition_rows = [dict(row) for row in appended]

        upserted = await db.execute(_UPSERT_STATE, *_state_params(result.state_row))
        state_rows = [dict(row) for row in upserted]

        attempted = 1 + (1 if result.transition_row is not None else 0)
        written = len(transition_rows) + len(state_rows)
        return {
            "event_kind": request.event_kind.value,
            "repository": result.state_row.repository,
            "pr_number": result.state_row.pr_number,
            "seq": result.state_row.seq,
            ROWS_UPSERTED_KEY: written,
            # Refused by the stale-write guard: an older seq, or a redelivered
            # transition. Not an error, and deliberately not counted as written.
            ROWS_REFUSED_KEY: attempted - written,
            "state_rows": state_rows,
            "transition_rows": transition_rows,
            "state_write_refused": not state_rows,
        }


__all__ = [
    "ROWS_REFUSED_KEY",
    "ROWS_UPSERTED_KEY",
    "TABLE_PR_LANDING_STATE",
    "TABLE_PR_LANDING_TRANSITIONS",
    "TOPIC_EVENT_KIND",
    "PrLandingProjectionWriter",
]
