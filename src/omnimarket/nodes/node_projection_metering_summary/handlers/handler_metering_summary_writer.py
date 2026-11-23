# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a writer: scope the Postgres pool to each runtime dispatch."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.handler_savings_priced_models_fill import (
    refresh_shared_priced_models,
)
from omnimarket.models.model_metering_summary_refresh_requested import (
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_metering_summary import (
    ModelMeteringSummaryFoldRequest,
    ModelMeteringSummaryRow,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_store_refresh import (
    ProtocolMeteringStoreReads,
    refresh_from_store,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
)
from omnimarket.nodes.node_projection_metering_summary.metering_refresh_coalescer import (
    MeteringRefreshCoalescer,
    coalesce_interval_from_overlay,
)
from omnimarket.nodes.node_projection_metering_summary.models.model_metering_refresh_coalesce import (
    ModelMeteringRefreshCoalesceStats,
)
from omnimarket.projection.protocol_database import ProtocolProjectionDatabaseSync
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
)
from omnimarket.projection.tenant_isolation import resolve_write_tenant

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
CONFLICT_KEY = "tenant_id,window_kind,window_start,baseline_model"
_COLUMNS = tuple(ModelMeteringSummaryRow.model_fields)
_UPSERT = (
    f"INSERT INTO public.metering_summary ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(f'${i}' for i in range(1, len(_COLUMNS) + 1))}) "
    f"ON CONFLICT ({CONFLICT_KEY}) DO UPDATE SET "
    + ", ".join(
        f"{col} = EXCLUDED.{col}"
        for col in _COLUMNS
        if col not in CONFLICT_KEY.split(",")
    )
    + " RETURNING tenant_id"
)


@lru_cache(maxsize=1)
def store_refresh_command_topic() -> str:
    """The store refresh command topic, as the node's contract names it."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    topic = str(contract["config"]["metering_summary"]["store_refresh_command_topic"])
    if topic not in contract["event_bus"]["subscribe_topics"]:
        raise ValueError(f"{_CONTRACT_PATH} names {topic!r} but does not subscribe it")
    return topic


@lru_cache(maxsize=1)
def coalesce_default_seconds() -> int:
    """The refresh spacing a deployment with no overlay runs at, as the contract names it."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    return int(
        contract["config"]["metering_summary"]["refresh_coalesce"][
            "default_min_interval_seconds"
        ]
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _coalesce_cells(stats: ModelMeteringRefreshCoalesceStats) -> dict[str, Any]:
    """The coalescer's counts as result cells: the visible half of 'never a silent skip'."""
    return {
        "refresh_runs_total": stats.runs,
        "refresh_coalesced_total": stats.coalesced,
        "refresh_pending_tenants": sorted(stats.pending),
    }


def store_rows(
    db: ProtocolProjectionDatabaseSync, rows: Sequence[ModelMeteringSummaryRow]
) -> None:
    """Replace snapshots through the existing store-neutral upsert boundary."""
    for row in rows:
        db.upsert("metering_summary", CONFLICT_KEY, row.model_dump(mode="json"))


class MeteringSummaryProjectionWriter(BaseProjectionRunner):
    """Accept a full snapshot request, fold it, and replace its stored rows."""

    onex_runtime_inprocess_dispatch = True
    # OMN-24492: class-level so a writer built without __init__ (the runtime's
    # in-process path and the existing tests do) still reads as "no coalescer yet".
    _coalescer: MeteringRefreshCoalescer | None = None
    _coalescer_lock = threading.Lock()
    _clock: Callable[[], datetime] | None = None

    def __init__(
        self,
        contract_path: Path | None = None,
        *,
        coalescer: MeteringRefreshCoalescer | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        path = contract_path or _CONTRACT_PATH
        self._contract: dict[str, Any] = yaml.safe_load(path.read_text())
        self._coalescer = coalescer
        if clock is not None:
            self._clock = clock

    @property
    def coalescer(self) -> MeteringRefreshCoalescer:
        """OMN-24492: the one coalescer of this writer, its spacing read once from the overlay.

        Built on the first refresh, not at construction, so a broken overlay refuses
        the refresh that needs it and never the contract load. No overlay reads as the
        contract's default (0: every refresh runs, as before).
        """
        with self._coalescer_lock:
            if self._coalescer is None:
                self._coalescer = MeteringRefreshCoalescer(
                    coalesce_interval_from_overlay(coalesce_default_seconds())
                )
            return self._coalescer

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["subscribe_topics"])

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["dlq_topics"])

    @property
    def store_refresh_topic(self) -> str:
        """OMN-17427: the command that names tenants and days, not runs."""
        return store_refresh_command_topic()

    def _is_store_refresh(self, topic: object, data: dict[str, Any]) -> bool:
        if isinstance(topic, str) and topic:
            return topic == self.store_refresh_topic
        # An in-process dispatch without the envelope's topic: the two commands
        # share no required field, so the shape names the command.
        return "refresh_id" in data and "records" not in data

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        # The in-process dispatch hands the handler the envelope's own context
        # beside the payload, and the set it adds is not closed: on the dev lane
        # it also carried `_db`, `_event_type`, `_envelope_id` and
        # `_envelope_timestamp`, which both command models forbid, so every
        # scheduled refresh was validated into the DLQ and metering_summary
        # stayed empty while delegation_events grew (OMN-17427). Neither model
        # declares a field starting with an underscore -- pydantic cannot -- so
        # the prefix, not a name list, is what separates context from payload.
        data = {
            k: v
            for k, v in request.payload.items()
            if k not in {"_topic", "_partition", "_offset", "_fallback_id"}
            and not k.startswith("_")
        }
        if self._is_store_refresh(request.topic, data):
            refresh = ModelMeteringSummaryRefreshRequested.model_validate(data)
            return asyncio.run(self._refresh_one_message(refresh))
        fold_request = ModelMeteringSummaryFoldRequest.model_validate(data)
        return asyncio.run(self._project_one_message(fold_request))

    async def _project_one_message(
        self, request: ModelMeteringSummaryFoldRequest
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            return await self._project(request)
        finally:
            await self.db.close()

    async def _refresh_one_message(
        self, request: ModelMeteringSummaryRefreshRequested
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            return await self._refresh(request)
        finally:
            await self.db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        if self._is_store_refresh(topic, data):
            await self._refresh(
                ModelMeteringSummaryRefreshRequested.model_validate(data)
            )
            return True
        await self._project(ModelMeteringSummaryFoldRequest.model_validate(data))
        return True

    def _reads_for(self, table: str) -> ProtocolMeteringStoreReads:
        return self.db_for(table, operation="read")

    async def _refresh(
        self, request: ModelMeteringSummaryRefreshRequested
    ) -> dict[str, Any]:
        """OMN-24492: run the refresh the coalescer admits, or count the one it holds back.

        Every refresh refolds every stored run of its tenant, so a burst of delegation
        terminals spent one fold each. A held-back refresh is never dropped silently: its
        days join the tenant's pending set, the next run carries them, and the held-back
        count and the tenants still pending ride in the result and the log.
        """
        coalescer = self.coalescer
        decision = coalescer.admit(request, (self._clock or _utc_now)())
        if decision.request is None:
            stats = coalescer.stats()
            logger.info(
                "metering refresh %s coalesced (%d held back in all, %d run); "
                "pending tenants: %s",
                request.refresh_id,
                stats.coalesced,
                stats.runs,
                sorted(stats.pending),
            )
            return {
                "refresh_id": request.refresh_id,
                "reason": request.reason.value,
                "refresh_coalesced": True,
                "rows_upserted": 0,
                **_coalesce_cells(stats),
            }
        try:
            # OMN-26444: the shared savings view prices runs from this table, so
            # it is refilled from the manifest before any run is read. The table
            # is not a db_io declaration of this contract: the topology grants
            # are derived from the contract set omnibase_infra pins, so a new
            # declaration here fails auto-wiring until that pin carries it. The
            # fill runs on the writer's own adapter, the same tenant_projection_writer
            # principal migration 102 grants on the table.
            await refresh_shared_priced_models(self.db_for("metering_summary"))
            result = await refresh_from_store(
                decision.request,
                reads_for=self._reads_for,
                fold_and_store=self._project,
            )
        except BaseException:
            coalescer.rollback(decision)
            raise
        coalescer.complete(decision)
        stats = coalescer.stats()
        logger.info(
            "metering refresh %s ran (%d run, %d held back in all); pending tenants: %s",
            request.refresh_id,
            stats.runs,
            stats.coalesced,
            sorted(stats.pending),
        )
        return {**result, "refresh_coalesced": False, **_coalesce_cells(stats)}

    async def _project(
        self, request: ModelMeteringSummaryFoldRequest
    ) -> dict[str, Any]:
        result = HandlerProjectionMeteringSummary().handle(request)
        count = 0
        for row in result.rows:
            values = row.model_dump(mode="json")
            # OMN-19513: the upsert names tenant_id, so the adapter refuses it
            # without the row's own tenant (OMN-15919). Unbound, every refresh
            # on the dev lane raised after folding, was redelivered, and, with
            # no `all` row ever stored, refolded every day of the tenant's runs
            # again: that loop held the main runtime's GIL ~70% of wall time
            # and starved every other projection on it, the hook-event
            # projection first (py-spy on the dev runtime, 2026-10-22T03:25Z).
            returned = await self.db_for("metering_summary").execute(
                _UPSERT,
                *(values[col] for col in _COLUMNS),
                tenant=resolve_write_tenant(
                    values["tenant_id"], table="metering_summary"
                ),
            )
            count += len(returned)
        return {
            "rows_upserted": count,
            "tenant_id": request.tenant_id,
            "baseline_model": request.baseline_model,
        }
