# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule 7a effect writer; the pure fold lives in pr_state_fold (OMN-19999)."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from omnimarket.nodes.contract_topics import contract_subscribe_topics
from omnimarket.nodes.node_projection_pr_state.contract_topics import (
    CONTRACT_PATH,
    SUBSCRIBE_TOPICS,
)
from omnimarket.nodes.node_projection_pr_state.handlers.pr_state_fold import (
    HandlerProjectionPrState,
)
from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_fold_request import (
    ModelPrStateFoldRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

_TABLE = "omninode_internal.pr_state"
_UPSERT = f"""
    INSERT INTO {_TABLE} (
        repo, pr_number, state, head_sha, base, head_ref, title, draft,
        author, author_is_bot, labels, armed, queued, watcher_class, ci_verdict,
        red_contexts, pending_contexts, ci_read_at, merged_at, observed_at, digest, last_digest
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13,
        $14, $15, $16::jsonb, $17::jsonb, $18, $19, $20, $21, $21
    )
    ON CONFLICT (repo, pr_number) DO UPDATE SET
        state = EXCLUDED.state, head_sha = EXCLUDED.head_sha,
        base = EXCLUDED.base, head_ref = EXCLUDED.head_ref, title = EXCLUDED.title,
        draft = EXCLUDED.draft, author = EXCLUDED.author, author_is_bot = EXCLUDED.author_is_bot,
        labels = EXCLUDED.labels, armed = EXCLUDED.armed, queued = EXCLUDED.queued,
        watcher_class = EXCLUDED.watcher_class, ci_verdict = EXCLUDED.ci_verdict,
        red_contexts = EXCLUDED.red_contexts, pending_contexts = EXCLUDED.pending_contexts,
        ci_read_at = EXCLUDED.ci_read_at, merged_at = EXCLUDED.merged_at,
        observed_at = EXCLUDED.observed_at, digest = EXCLUDED.digest, last_digest = EXCLUDED.last_digest
    WHERE ({_TABLE}.observed_at, {_TABLE}.last_digest)
        < (EXCLUDED.observed_at, EXCLUDED.last_digest)
"""
_T = TypeVar("_T")


class PrStateProjectionWriter(BaseProjectionRunner):
    """Persist a complete observation under the SQL ordering guard."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        self._contract_path = contract_path or CONTRACT_PATH
        self._dispatch_lock = threading.Lock()

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def subscribe_topics(self) -> list[str]:
        declared = contract_subscribe_topics(self._contract_path)
        if declared != SUBSCRIBE_TOPICS:
            raise ValueError("contract subscribe_topics disagree with PR state topics")
        return list(declared)

    def handle(self, input_data: dict[str, object]) -> dict[str, int]:
        data = dict(input_data)
        topic = str(data.pop("_topic", self.subscribe_topics[0]))
        meta = MessageMeta(
            partition=int(str(data.pop("_partition", 0))),
            offset=int(str(data.pop("_offset", 0))),
            fallback_id=str(data.pop("_fallback_id", "")),
            topic=topic,
        )
        with self._dispatch_lock:
            written = self._run(self._project_one_message(topic, data, meta))
        return {"rows_upserted": written}

    async def _project_one_message(
        self, topic: str, data: dict[str, object], meta: MessageMeta
    ) -> int:
        try:
            await self.db.connect()
            return await self._project_and_report(topic, data, meta)
        finally:
            try:
                await self._stop_producer()
            finally:
                await self.db.close()

    @staticmethod
    def _run(coro: Coroutine[object, object, _T]) -> _T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    async def project_event(
        self, topic: str, data: dict[str, object], meta: MessageMeta
    ) -> bool:
        await self._project_and_report(topic, data, meta)
        return True

    async def _project_and_report(
        self, topic: str, data: dict[str, object], meta: MessageMeta
    ) -> int:
        if topic not in SUBSCRIBE_TOPICS:
            raise ValueError(f"unsubscribed topic {topic!r}")
        result = HandlerProjectionPrState().handle(
            ModelPrStateFoldRequest.model_validate(data)
        )
        e = result.event
        await self.db.execute(
            _UPSERT,
            e.repo,
            e.pr_number,
            e.state.value,
            e.head_sha,
            e.base,
            e.head_ref,
            e.title,
            e.draft,
            e.author,
            e.author_is_bot,
            json.dumps(e.labels),
            e.armed,
            e.queued,
            e.watcher_class,
            e.ci_verdict,
            json.dumps(e.red_contexts),
            json.dumps(e.pending_contexts),
            e.ci_read_at,
            e.merged_at,
            result.observed_at,
            e.digest,
        )
        # Like the work-ledger writer, acknowledge one durable materialization
        # even when the guard preserves an existing newer row on replay.
        return 1
