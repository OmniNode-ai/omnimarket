# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a effect writer of the eval-results projection (OMN-24171).

Dispatched in process by the runtime (``onex_runtime_inprocess_dispatch``). For each delivery it
reads the rows the pure fold names (``read_plan``), calls the fold, writes what the fold decided
and, when a table changed, republishes the compact snapshot. A runtime tick republishes it too,
stamped with the tick's time, so a quiet projection still proves it is publishing; every run row
keeps its own last ``scored_at``, so a republished old result never reads as newly tested.

A snapshot over its byte bound, or with a text field over its length, is refused with a typed
error and not published; the tables are already written, so a refusal never loses an item.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from omnimarket.enums.enum_eval_lane import EnumEvalLane
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_eval_results.handlers.eval_results_snapshot import (
    build_snapshot,
)
from omnimarket.nodes.node_projection_eval_results.handlers.eval_results_store import (
    EvalResultsStore,
)
from omnimarket.nodes.node_projection_eval_results.handlers.handler_projection_eval_results import (
    HandlerProjectionEvalResults,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    SOURCE_TOPICS,
    TOPIC_SNAPSHOT,
    TOPIC_TICK,
    ModelEvalProjectionRequest,
    ModelEvalReadPlan,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRefusal,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_snapshot import (
    EvalSnapshotRefusedError,
    ModelEvalResultsOverlay,
)
from omnimarket.projection.dlq import correlation_id_from_payload, route_to_dlq
from omnimarket.projection.handler_shim import (
    INJECTED_ENVELOPE_TIMESTAMP_KEY,
    INJECTED_TENANT_ID_KEY,
    RUNTIME_INJECTED_KEYS,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta
from omnimarket.projection.tenant_isolation import house_tenant_write_stamp
from omnimarket.topic_namespace import apply_topic_namespace

logger = logging.getLogger(__name__)

OVERLAY_ID = "node_projection_eval_results"
#: Refusals that name a malformed delivery or a mapping gap: they go to the contract's DLQ topic.
#: A control row, an unscored row and a call that is not a task are skipped by design, not malformed.
_DLQ_REFUSALS = frozenset(
    {
        EnumEvalRefusal.INVALID_EVENT,
        EnumEvalRefusal.INVALID_ITEM,
        EnumEvalRefusal.TRACE_DIGEST_MISMATCH,
        EnumEvalRefusal.UNMAPPED_TIER,
        EnumEvalRefusal.UNMAPPED_PROVIDER,
    }
)
_TENANT_TABLE = "eval_item_scores"


def _instant(value: Any) -> datetime:
    """A tz-aware instant from an envelope or tick value; refuses a missing or naive one."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"event time {value!r} is missing or has no timezone")
    return value.astimezone(UTC)


class EvalResultsProjectionWriter(BaseProjectionRunner):
    """One event loop and pool per in-process dispatch, explicit tenant context."""

    onex_runtime_inprocess_dispatch = True

    def __init__(
        self,
        contract_path: Path | None = None,
        *,
        overlay: ModelEvalResultsOverlay | None = None,
        clock: Callable[[], datetime] | None = None,
        store_factory: Callable[[Any], Any] = EvalResultsStore,
    ) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        self._contract: dict[str, Any] = yaml.safe_load(path.read_text())
        self._fold = HandlerProjectionEvalResults()
        self._overlay = overlay
        self._clock = clock or (lambda: datetime.now(UTC))
        self._store_factory = store_factory
        self._last_published_at: datetime | None = None
        self._run_locks: dict[tuple[str, str], tuple[asyncio.Lock, int]] = {}

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["subscribe_topics"])

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        return list(self._contract["event_bus"].get("dlq_topics", []))

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        publish = await self.get_publish_fn()
        if publish is None:
            raise RuntimeError(f"no DLQ publisher configured for {topic}")
        await publish(topic, value)

    def overlay(self) -> ModelEvalResultsOverlay:
        """The node's overlay, read on first use; refuses when none supplies it."""
        if self._overlay is None:
            self._overlay = load_structured_overlay(OVERLAY_ID, ModelEvalResultsOverlay)
        return self._overlay

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        topic = request.topic_or(self.subscribe_topics[0])
        meta = request.message_meta(topic)
        return asyncio.run(
            self._project_one_message(topic, dict(request.payload), meta)
        )

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            return await self._project(topic, data, meta)
        finally:
            await self.db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project(topic, data, meta)
        return True

    def _default_tenant(self, data: dict[str, Any]) -> str:
        """The envelope's tenant, else the lane's configured one, else the house tenant, as a uuid."""
        tenant = (
            data.get(INJECTED_TENANT_ID_KEY)
            or house_tenant_write_stamp(table=_TENANT_TABLE)["tenant_id"]
        )
        return str(UUID(str(tenant)))

    async def _project(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        if topic == TOPIC_TICK:
            return await self._tick(data)
        if topic not in SOURCE_TOPICS:
            raise ValueError(
                f"{topic} is not a source topic of the eval-results projection"
            )
        overlay = self.overlay()
        payload = {k: v for k, v in data.items() if k not in RUNTIME_INJECTED_KEYS}
        tenant = self._default_tenant(data)
        observed_at = _instant(data.get(INJECTED_ENVELOPE_TIMESTAMP_KEY))
        plan = self._fold.read_plan(topic, payload, tenant)
        store = self._store_factory(self.db)
        async with self._one_at_a_time(plan):
            items, record, parts = await store.load(plan)
            result = self._fold.handle(
                ModelEvalProjectionRequest(
                    topic=topic,
                    payload=payload,
                    observed_at=observed_at,
                    tenant_id=tenant,
                    lane=EnumEvalLane(overlay.lane),
                    host=overlay.host,
                    install_revision=overlay.install_revision,
                    standing_items=items,
                    standing_run=record,
                    standing_parts=parts,
                )
            )
            counts = await store.apply(plan, result)
        routed = 0
        for refusal in result.refused:
            if refusal.reason in _DLQ_REFUSALS:
                routed += int(
                    await route_to_dlq(
                        publish=await self.get_publish_fn(),
                        dlq_topics=self.poison_dlq_topics,
                        original_message=payload,
                        failure_reason=f"{refusal.reason.value}: {refusal.detail}",
                        handler=type(self).__name__,
                        correlation_id=correlation_id_from_payload(
                            payload, fallback=refusal.subject
                        ),
                    )
                )
        outcome: dict[str, Any] = {
            "rows_upserted": sum(counts.values()),
            **{f"{name}_written": n for name, n in counts.items()},
            "refused": [r.model_dump(mode="json") for r in result.refused],
            "dlq_routed": routed,
        }
        if result.rates or result.items or result.run_record is not None:
            outcome["snapshot"] = await self._publish(
                store, tenant, published_at=self._clock()
            )
        return outcome

    @asynccontextmanager
    async def _one_at_a_time(self, plan: ModelEvalReadPlan) -> AsyncIterator[None]:
        """Hold one run's read, fold and write apart from any other delivery of the same run here.

        A rate is recounted from the rows read before the write, so two deliveries of one run that
        overlap would each recount without the other's item and the second write would drop it.
        This serialises deliveries inside this process; it does not span runtime replicas.
        """
        if not plan.run_id:
            yield
            return
        key = (plan.tenant_id, plan.run_id)
        lock, waiting = self._run_locks.get(key, (asyncio.Lock(), 0))
        self._run_locks[key] = (lock, waiting + 1)
        try:
            async with lock:
                yield
        finally:
            lock, waiting = self._run_locks[key]
            if waiting <= 1:
                del self._run_locks[key]
            else:
                self._run_locks[key] = (lock, waiting - 1)

    async def _tick(self, data: dict[str, Any]) -> dict[str, Any]:
        """Republish the snapshot at the tick's time once per publish interval; reads no other clock."""
        now = _instant(data.get("now") or data.get(INJECTED_ENVELOPE_TIMESTAMP_KEY))
        interval = self.overlay().publish_interval_s
        last = self._last_published_at
        if last is not None and (now - last).total_seconds() < interval:
            return {
                "rows_upserted": 0,
                "snapshot": {"published": False, "reason": "interval"},
            }
        snapshot = await self._publish(
            self._store_factory(self.db), self._default_tenant(data), published_at=now
        )
        return {"rows_upserted": 0, "snapshot": snapshot}

    async def _publish(
        self, store: Any, tenant: str, *, published_at: datetime
    ) -> dict[str, Any]:
        rates, records = await store.snapshot_inputs(tenant)
        try:
            snapshot = build_snapshot(
                tenant_id=tenant,
                published_at=published_at,
                rates=rates,
                records=records,
                overlay=self.overlay(),
            )
        except EvalSnapshotRefusedError as refusal:
            logger.warning("eval-results snapshot refused: %s", refusal)
            return {
                "published": False,
                "refused": refusal.kind.value,
                "size": refusal.size,
                "limit": refusal.limit,
            }
        publish = await self.get_publish_fn()
        if publish is None:
            logger.warning("eval-results snapshot built but no publish transport")
            return {"published": False, "reason": "no_transport"}
        await publish(
            apply_topic_namespace(TOPIC_SNAPSHOT),
            snapshot.model_dump_json().encode("utf-8"),
        )
        self._last_published_at = published_at
        return {"published": True, "published_at": published_at.isoformat()}


__all__ = ["EvalResultsProjectionWriter"]
