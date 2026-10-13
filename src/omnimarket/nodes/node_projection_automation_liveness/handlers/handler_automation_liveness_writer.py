# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime-facing writer of the automation-liveness projection.

THE TWO-CLASS SHAPE
    The runtime calls this entry expecting a write, with the transport
    coordinates beside the domain payload. A pure fold cannot satisfy that
    protocol, so this writer reads the rows the event can touch, calls the same
    typed fold the tests call, and persists the rows that fold changed. The
    runtime path and the definition-B computation cannot disagree about a row.

WHAT IT REPORTS
    ``rows_upserted`` counts the rows the statements returned, not the offsets
    the consumer committed. A replayed event changes no row and reports zero.

THE STORE
    Postgres through the runtime's ``AsyncpgAdapter``, or the local SQLite file
    when the projection runtime binding names one. A test or an embedding may
    hand a store in.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

import yaml
from pydantic import BaseModel, TypeAdapter

from omnimarket.models.liveness.model_automation_liveness import (
    EVENT_PAYLOAD_MODELS,
    EnumAutomationLivenessEvent,
    ModelAutomationAlarmCleared,
    ModelAutomationAlarmDelivered,
    ModelAutomationAlarmRaised,
    ModelAutomationAlarmRecorded,
    ModelAutomationLivenessDeclared,
    ModelAutomationRunObserved,
    automation_liveness_topics,
)
from omnimarket.nodes.node_projection_automation_liveness.handlers.handler_projection_automation_liveness import (
    HandlerProjectionAutomationLiveness,
)
from omnimarket.nodes.node_projection_automation_liveness.models import (
    AutomationLivenessEvent,
    ModelAutomationAlarmEpisodeRow,
    ModelAutomationLivenessFoldRequest,
    ModelAutomationLivenessSnapshot,
    ModelAutomationLivenessStateRow,
    ModelAutomationRunRow,
    state_key_text,
)
from omnimarket.nodes.node_projection_automation_liveness.ports.liveness_store import (
    ProtocolLivenessStore,
    SqliteLivenessStore,
    StoreValue,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.handler_shim import RUNTIME_INJECTED_KEYS
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    deterministic_correlation_id,
)
from omnimarket.projection.sqlite_database import (
    SQLITE_SCHEMES,
    sqlite_path_from_dsn,
)

logger = logging.getLogger(__name__)

SCHEMA = "omninode_internal"
STATE_TABLE = "automation_liveness_state"
RUN_TABLE = "automation_run_history"
EPISODE_TABLE = "automation_alarm_episodes"

#: Most recent run rows read to recompute a process's run-derived fields. The
#: idle streak and the failure window are computed over these, so a streak
#: longer than this reads as this long.
RUN_HISTORY_READ_LIMIT = 500

_STATE_FIELDS = tuple(ModelAutomationLivenessStateRow.model_fields)
_RUN_FIELDS = tuple(ModelAutomationRunRow.model_fields)
_EPISODE_FIELDS = tuple(ModelAutomationAlarmEpisodeRow.model_fields)

_STATE_COLUMNS = ("process_key", *_STATE_FIELDS, "projected_at")
_RUN_COLUMNS = ("run_key", *_RUN_FIELDS, "projected_at")
_EPISODE_COLUMNS = (*_EPISODE_FIELDS, "projected_at")

_T = TypeVar("_T")

_EVENT_ADAPTER: TypeAdapter[AutomationLivenessEvent] = TypeAdapter(
    AutomationLivenessEvent
)


def _relation(store: ProtocolLivenessStore, table: str) -> str:
    """The table as the store names it: schema-qualified on Postgres, bare on SQLite."""
    return table if isinstance(store, SqliteLivenessStore) else f"{SCHEMA}.{table}"


def _upsert(relation: str, columns: Sequence[str], key: str) -> str:
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != key)
    return (
        f"INSERT INTO {relation} ({', '.join(columns)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT ({key}) DO UPDATE SET {assignments} "
        f"RETURNING {key}"
    )


_KEY_COLUMNS = {
    STATE_TABLE: ("process_key", _STATE_COLUMNS),
    RUN_TABLE: ("run_key", _RUN_COLUMNS),
    EPISODE_TABLE: ("episode_id", _EPISODE_COLUMNS),
}


def _select(
    store: ProtocolLivenessStore, table: str, columns: Sequence[str], tail: str
) -> str:
    return f"SELECT {', '.join(columns)} FROM {_relation(store, table)} {tail}"


def _placeholders(count: int, start: int = 1) -> str:
    return ", ".join(f"${i}" for i in range(start, start + count))


def _topic_kinds() -> Mapping[str, EnumAutomationLivenessEvent]:
    return MappingProxyType(
        {topic: kind for kind, topic in automation_liveness_topics().items()}
    )


def _bind(value: object) -> StoreValue:
    """A column value in the form both stores accept."""
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"no column binding for {type(value).__name__}")


def _row_values(
    row: BaseModel, key_column: str | None, key: str | None, columns: Sequence[str]
) -> dict[str, StoreValue]:
    values = {name: _bind(value) for name, value in row}
    if key_column is not None:
        values[key_column] = key
    return {name: values[name] for name in columns if name in values}


def _served(values: Mapping[str, StoreValue]) -> dict[str, Any]:
    """A stored row in the JSON form a snapshot delta and a typed read carry."""
    return {
        name: value.isoformat() if isinstance(value, datetime) else value
        for name, value in values.items()
    }


def _scope(
    event: AutomationLivenessEvent,
) -> tuple[list[tuple[str, str]], list[UUID], tuple[str, str] | None]:
    """The state keys, episode ids and run-history process the event can touch."""
    if isinstance(event, ModelAutomationLivenessDeclared):
        return ([(e.process_id, e.host) for e in event.overlay.processes], [], None)
    if isinstance(event, ModelAutomationRunObserved):
        key = (event.process_id, event.host)
        return ([key], [], key)
    if isinstance(event, ModelAutomationAlarmRaised | ModelAutomationAlarmCleared):
        return ([(event.process_id, event.host)], [event.episode_id], None)
    if isinstance(event, ModelAutomationAlarmDelivered | ModelAutomationAlarmRecorded):
        return ([], [event.episode_id], None)
    return ([(event.process_id, event.host)], [], None)


class AutomationLivenessProjectionWriter(BaseProjectionRunner):
    """Folds each seam event against its stored rows and persists the changes."""

    #: The shared runtime dispatches this writer once per message in process.
    #: Without this declaration a runner-shaped class is treated as standalone
    #: and the runtime can consume its topics without invoking a write.
    onex_runtime_inprocess_dispatch = True

    def __init__(
        self,
        contract_path: Path | None = None,
        *,
        store: ProtocolLivenessStore | None = None,
    ) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        exposures = load_projection_exposures_from_contract(
            self._contract,
            str(self._contract.get("name", "projection_automation_liveness")),
            path,
        )
        self._snapshot_exposures: dict[str, ProjectionTableConfig] = {
            e.table: e for e in exposures if e.bus_backed
        }
        self._fold = HandlerProjectionAutomationLiveness()
        self._injected_store = store
        self._dispatch_lock = threading.Lock()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        """Read the quarantine topic from the contract, never restate it here."""
        return list(self._contract.get("event_bus", {}).get("dlq_topics", []))

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        publish = await self.get_publish_fn()
        if publish is None:
            raise RuntimeError(f"no publisher available for liveness DLQ {topic}")
        await publish(topic, value)

    def _store(self) -> ProtocolLivenessStore:
        if self._injected_store is not None:
            return self._injected_store
        binding = self._runtime_binding
        if binding is not None:
            url = binding.resolve_database_url()
            if urlsplit(url).scheme.lower() in SQLITE_SCHEMES:
                return SqliteLivenessStore(sqlite_path_from_dsn(url))
        return self.db

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal shim: one injected message, one fold, one write."""
        data = dict(input_data)
        topics = self.subscribe_topics
        topic = str(data.get("_topic", topics[0] if topics else ""))
        partition = int(data.get("_partition", 0))
        offset = int(data.get("_offset", 0))
        fallback_id = str(data.get("_fallback_id", "")) or (
            deterministic_correlation_id(topic, partition, offset)
        )
        payload = {
            k: v
            for k, v in data.items()
            if k not in RUNTIME_INJECTED_KEYS and k != "_fallback_id"
        }
        meta = MessageMeta(
            partition=partition, offset=offset, fallback_id=fallback_id, topic=topic
        )
        with self._dispatch_lock:
            return self._run(self._project_one_message(topic, payload, meta))

    @staticmethod
    def _run(coro: Coroutine[Any, Any, _T]) -> _T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        store = self._store()
        await store.connect()
        try:
            return await self._project(store, topic, data, meta)
        finally:
            await self._stop_producer()
            await store.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone entrypoint: connect, project, report consumed."""
        store = self._store()
        await store.connect()
        try:
            await self._project(store, topic, data, meta)
        finally:
            await store.close()
        return True

    def _event(self, topic: str, data: dict[str, Any]) -> AutomationLivenessEvent:
        kind = _topic_kinds().get(topic)
        if kind is None:
            raise ValueError(f"unsubscribed topic {topic!r}")
        model = EVENT_PAYLOAD_MODELS[kind]
        # Validate against the topic's own model, so a payload of another
        # event is refused rather than accepted by the union.
        return _EVENT_ADAPTER.validate_python(model.model_validate(data))

    async def _project(
        self,
        store: ProtocolLivenessStore,
        topic: str,
        data: dict[str, Any],
        meta: MessageMeta,
    ) -> dict[str, Any]:
        event = self._event(topic, data)
        prior = await self._load_prior(store, event)
        changes = self._fold.handle(
            ModelAutomationLivenessFoldRequest(event=event, prior=prior)
        ).changes
        projected_at = datetime.now(UTC)
        written: list[dict[str, Any]] = []
        for state in changes.states:
            values = _row_values(
                state, "process_key", state.process_key, _STATE_COLUMNS
            )
            values["projected_at"] = projected_at
            written += await self._write(store, STATE_TABLE, values, meta)
        for run in changes.runs:
            values = _row_values(run, "run_key", run.run_key, _RUN_COLUMNS)
            values["projected_at"] = projected_at
            written += await self._write(store, RUN_TABLE, values, meta)
        for episode in changes.episodes:
            values = _row_values(
                episode, "episode_id", episode.episode_key, _EPISODE_COLUMNS
            )
            values["projected_at"] = projected_at
            written += await self._write(store, EPISODE_TABLE, values, meta)
        return {"rows_upserted": len(written), "automation_liveness_rows": written}

    async def _write(
        self,
        store: ProtocolLivenessStore,
        table: str,
        values: dict[str, StoreValue],
        meta: MessageMeta,
    ) -> list[dict[str, Any]]:
        key, columns = _KEY_COLUMNS[table]
        statement = _upsert(_relation(store, table), columns, key)
        returned = await store.execute(statement, *(values[c] for c in columns))
        if not returned:
            return []
        served = _served(values)
        exposure = self._snapshot_exposures.get(table)
        if exposure is not None:
            await self.publish_snapshot_delta(
                exposure,
                op="upsert",
                row=served,
                source_event_id=meta.fallback_id,
                source_topic=meta.topic,
                source_partition=meta.partition,
                source_offset=meta.offset,
            )
        return [{"relation": table, **served}]

    async def _load_prior(
        self, store: ProtocolLivenessStore, event: AutomationLivenessEvent
    ) -> ModelAutomationLivenessSnapshot:
        state_keys, episode_ids, run_process = _scope(event)
        states = await self._load_states(store, state_keys)
        if isinstance(event, ModelAutomationAlarmRaised):
            # The raise decides against the episode the row holds open.
            episode_ids += [
                s.open_episode_id for s in states if s.open_episode_id is not None
            ]
        episodes = await self._load_episodes(store, episode_ids)
        runs = (
            await self._load_runs(store, run_process, event)
            if run_process is not None and isinstance(event, ModelAutomationRunObserved)
            else ()
        )
        return ModelAutomationLivenessSnapshot(
            states=states, runs=runs, episodes=episodes
        )

    @staticmethod
    async def _load_states(
        store: ProtocolLivenessStore, keys: Sequence[tuple[str, str]]
    ) -> tuple[ModelAutomationLivenessStateRow, ...]:
        if not keys:
            return ()
        texts = sorted({state_key_text(p, h) for p, h in keys})
        rows = await store.execute(
            _select(
                store,
                STATE_TABLE,
                _STATE_FIELDS,
                f"WHERE process_key IN ({_placeholders(len(texts))})",
            ),
            *texts,
        )
        return tuple(ModelAutomationLivenessStateRow.model_validate(r) for r in rows)

    @staticmethod
    async def _load_episodes(
        store: ProtocolLivenessStore, ids: Sequence[UUID]
    ) -> tuple[ModelAutomationAlarmEpisodeRow, ...]:
        unique = sorted({str(i) for i in ids})
        if not unique:
            return ()
        rows = await store.execute(
            _select(
                store,
                EPISODE_TABLE,
                _EPISODE_FIELDS,
                f"WHERE episode_id IN ({_placeholders(len(unique))})",
            ),
            *unique,
        )
        return tuple(ModelAutomationAlarmEpisodeRow.model_validate(r) for r in rows)

    @staticmethod
    async def _load_runs(
        store: ProtocolLivenessStore,
        process: tuple[str, str],
        event: ModelAutomationRunObserved,
    ) -> tuple[ModelAutomationRunRow, ...]:
        recent = await store.execute(
            _select(
                store,
                RUN_TABLE,
                _RUN_FIELDS,
                "WHERE process_id = $1 AND host = $2 "
                "ORDER BY COALESCE(finished_at, started_at) DESC LIMIT $3",
            ),
            process[0],
            process[1],
            RUN_HISTORY_READ_LIMIT,
        )
        # The row this event replaces, even when it is older than the window.
        same = await store.execute(
            _select(store, RUN_TABLE, _RUN_FIELDS, "WHERE run_key = $1"),
            "#".join((*process, event.run_id, event.phase.value)),
        )
        rows = {
            ModelAutomationRunRow.model_validate(
                r
            ).key: ModelAutomationRunRow.model_validate(r)
            for r in (*recent, *same)
        }
        return tuple(rows[k] for k in sorted(rows))


__all__ = [
    "EPISODE_TABLE",
    "RUN_HISTORY_READ_LIMIT",
    "RUN_TABLE",
    "STATE_TABLE",
    "AutomationLivenessProjectionWriter",
]
