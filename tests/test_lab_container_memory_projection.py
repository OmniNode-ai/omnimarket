# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19961 (task 5b of the .202 memory hardening plan): the lane container
memory event is projected into ``omninode_internal.lab_container_memory_window``.

The fixture is task 5's own event fixture (omnibase_infra
``tests/unit/scripts/fixtures/lane_container_memory/event.v1.json``, OMN-19959),
copied byte for byte: two records on host ``omnipc2``, one Redpanda container
with no memory limit and one runtime container that was OOM-killed, plus two
CI runs, one still running.

Rule 7a is exercised as two classes: a pure fold that returns rows and writes
nothing, and an effect-class writer that persists the fold's rows through an
``ON CONFLICT (record_key)`` upsert. The writer is driven through its
synchronous ``handle()`` twice, because that is the entry the runtime calls
once per consumed message and one call cannot expose a loop-bound pool cached
across messages.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_projection_lab_container_memory.handlers.handler_container_memory_fold import (
    HandlerContainerMemoryFold,
)
from omnimarket.nodes.node_projection_lab_container_memory.handlers.handler_container_memory_writer import (
    TABLE,
    LabContainerMemoryProjectionWriter,
)
from omnimarket.nodes.node_projection_lab_container_memory.models import (
    ModelContainerMemoryFoldResult,
    ModelLaneContainerMemoryEvent,
)
from omnimarket.projection.error_classification import (
    ProjectionErrorClass,
    classify_projection_error,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "lab_container_memory"
    / "event.v1.json"
)
_NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_lab_container_memory"
)
_IN_TOPIC = "onex.evt.omnibase-infra.lane-container-memory.v1"
_APPLIED_TOPIC = "onex.evt.omnimarket.projection-lab-container-memory-applied.v1"
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-lab-container-memory-malformed.v1"


def _event() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return loaded


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (_NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return loaded


# ---------------------------------------------------------------------------
# The pure fold
# ---------------------------------------------------------------------------


def _fold(event: dict[str, Any]) -> ModelContainerMemoryFoldResult:
    return HandlerContainerMemoryFold().handle(
        ModelLaneContainerMemoryEvent.model_validate(event)
    )


def test_the_fold_returns_one_row_per_record_carrying_its_record_key() -> None:
    event = _event()
    result = _fold(event)

    assert len(result.rows) == 2
    assert {row.record_key for row in result.rows} == {
        record["record_key"] for record in event["records"]
    }
    by_name = {row.container_name: row for row in result.rows}
    redpanda = by_name["omnibase-infra-sim-202-redpanda"]
    assert redpanda.host == "omnipc2"
    assert redpanda.lane == "sim-202"
    assert redpanda.limit_bytes is None
    assert redpanda.peak_bytes == 2114498560
    effects = by_name["omninode-sim-202-runtime-effects"]
    assert effects.limit_bytes == 268435456
    assert effects.oom_kill_delta == 1
    assert effects.max_total == 1060


def test_the_fold_is_deterministic() -> None:
    first = _fold(_event())
    second = _fold(_event())
    assert first == second
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_every_row_carries_the_window_the_peak_window_and_the_ci_runs() -> None:
    event = _event()
    result = _fold(event)
    run_ids = {run["run_id"] for run in event["ci_runs"]}
    for row in result.rows:
        assert row.host_boot_id == event["host_boot_id"]
        assert row.window_start.isoformat().startswith("2026-09-28T18:00:00")
        assert row.window_end.isoformat().startswith("2026-09-28T19:00:00")
        # A peak is since the container started, never per pass.
        assert row.peak_window_start == row.container_started_at
        assert row.peak_window_end == row.window_end
        assert {run.run_id for run in row.ci_runs} == run_ids
    running = [run for run in result.rows[0].ci_runs if run.job_completed_at is None]
    assert [run.repo for run in running] == ["OmniNode-ai/omnimarket"]


def test_the_fold_is_the_pure_half_and_the_writer_the_effect_half() -> None:
    """Rule 7a: the writer is dispatched in-process; the fold never is."""
    assert LabContainerMemoryProjectionWriter.onex_runtime_inprocess_dispatch is True
    assert not getattr(
        HandlerContainerMemoryFold, "onex_runtime_inprocess_dispatch", False
    )
    fold_source = (
        _NODE_DIR / "handlers" / "handler_container_memory_fold.py"
    ).read_text(encoding="utf-8")
    assert "ModelEventEnvelope" not in fold_source
    assert "ModelHandlerOutput" not in fold_source


def test_an_event_with_two_records_under_one_key_is_malformed() -> None:
    event = _event()
    event["records"][1]["record_key"] = event["records"][0]["record_key"]
    with pytest.raises(ValidationError):
        ModelLaneContainerMemoryEvent.model_validate(event)


def test_an_unknown_schema_major_is_refused() -> None:
    event = _event()
    event["schema_version"] = "2.0.0"
    with pytest.raises(ValidationError):
        ModelLaneContainerMemoryEvent.model_validate(event)


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


class _LoopBoundPool:
    """Reduced asyncpg: usable only from the loop that created it."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.closed = False

    def check(self) -> None:
        if self.closed:
            raise RuntimeError("pool is closed")
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Event loop is closed")


class _KeyedTableAdapter:
    """Stands in for ``AsyncpgAdapter`` over one table keyed on ``record_key``.

    It enforces the adapter's loop affinity and applies the writer's upsert
    the way the primary key does: the first parameter is ``record_key``, and a
    second insert under the same key replaces the row rather than adding one.
    The SQL itself is proven against real Postgres in
    ``tests/test_lab_container_memory_real_postgres.py``.
    """

    def __init__(self) -> None:
        self._pool: _LoopBoundPool | None = None
        self.rows: dict[str, tuple[Any, ...]] = {}
        self.calls: list[str] = []
        self.connects = 0

    async def connect(self) -> None:
        self.connects += 1
        self._pool = _LoopBoundPool()

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.closed = True
            self._pool = None

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self._pool is not None, "call connect() first"
        self._pool.check()
        self.calls.append(query)
        assert f"INSERT INTO {TABLE}" in query
        assert "ON CONFLICT (record_key) DO UPDATE" in query
        self.rows[str(params[0])] = params
        return [{"record_key": params[0]}]


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> LabContainerMemoryProjectionWriter:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")
    instance = LabContainerMemoryProjectionWriter()
    instance._db = _KeyedTableAdapter()  # type: ignore[assignment]
    return instance


def _message(event: dict[str, Any], offset: int) -> dict[str, Any]:
    message = copy.deepcopy(event)
    message["_topic"] = _IN_TOPIC
    message["_partition"] = 0
    message["_offset"] = offset
    return message


def test_the_writer_applied_twice_leaves_exactly_one_row_per_record_key(
    writer: LabContainerMemoryProjectionWriter,
) -> None:
    adapter: _KeyedTableAdapter = writer._db  # type: ignore[assignment]
    event = _event()
    keys = {record["record_key"] for record in event["records"]}

    first = writer.handle(_message(event, 9))
    assert first["rows_upserted"] == 2
    assert set(first["record_keys"]) == keys
    # The table is not empty after the first apply.
    assert set(adapter.rows) == keys

    second = writer.handle(_message(event, 9))
    assert second["rows_upserted"] == 2
    assert set(adapter.rows) == keys
    assert len(adapter.rows) == len(keys)
    # Two messages, two loops, and no pool carried between them.
    assert adapter.connects == 2
    assert adapter._pool is None


def test_the_writer_persists_the_rows_the_fold_derives(
    writer: LabContainerMemoryProjectionWriter,
) -> None:
    adapter: _KeyedTableAdapter = writer._db  # type: ignore[assignment]
    event = _event()
    writer.handle(_message(event, 9))
    fold = _fold(event)
    for row in fold.rows:
        params = adapter.rows[row.record_key]
        assert params[1:18] == (
            row.host,
            row.host_boot_id,
            row.lane,
            row.container_id,
            row.container_name,
            row.container_started_at,
            row.limit_bytes,
            row.peak_bytes,
            row.peak_window_start,
            row.peak_window_end,
            row.window_start,
            row.window_end,
            row.max_total,
            row.max_delta,
            row.oom_kill_total,
            row.oom_kill_delta,
            json.dumps(
                [run.model_dump(mode="json") for run in row.ci_runs], sort_keys=True
            ),
        )


def test_an_event_missing_peak_bytes_is_poison_and_writes_no_row(
    writer: LabContainerMemoryProjectionWriter,
) -> None:
    adapter: _KeyedTableAdapter = writer._db  # type: ignore[assignment]
    event = _event()
    del event["records"][0]["peak_bytes"]

    with pytest.raises(ValidationError) as caught:
        writer.handle(_message(event, 10))

    # POISON is the class the runtime's in-process path and the base runner
    # both route to the contract DLQ; a RECOVERABLE error would retry forever.
    assert classify_projection_error(caught.value) is ProjectionErrorClass.POISON
    assert adapter.rows == {}
    assert adapter.calls == []
    assert writer.poison_dlq_topics == [_DLQ_TOPIC]


def test_the_standalone_path_routes_a_missing_peak_to_the_dlq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")
    published: list[tuple[str, bytes]] = []

    async def _publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    instance = LabContainerMemoryProjectionWriter(publish_fn=_publish)
    adapter = _KeyedTableAdapter()
    instance._db = adapter  # type: ignore[assignment]
    event = _event()
    del event["records"][1]["peak_bytes"]
    meta = MessageMeta(partition=0, offset=11, fallback_id="fixture", topic=_IN_TOPIC)

    async def _drive() -> bool:
        try:
            await instance.project_event(_IN_TOPIC, event, meta)
        except ValidationError as err:
            return await instance._route_poison_to_dlq(_IN_TOPIC, event, err, meta)
        raise AssertionError("a record without peak_bytes was projected")

    assert asyncio.run(_drive()) is True
    assert [topic for topic, _ in published] == [_DLQ_TOPIC]
    assert adapter.rows == {}


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_the_contract_declares_the_topics_the_table_and_the_key() -> None:
    contract = _contract()
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [_IN_TOPIC]
    assert bus["publish_topics"] == [_APPLIED_TOPIC]
    assert bus["dlq_topics"] == [_DLQ_TOPIC]
    assert contract["terminal_event"] == _APPLIED_TOPIC

    tables = contract["db_io"]["db_tables"]
    assert [(t["schema"], t["name"]) for t in tables] == [
        ("omninode_internal", "lab_container_memory_window")
    ]
    assert contract["db_io"]["idempotency_key"] == "record_key"
    assert contract["runtime_lanes"] == ["compose-dev", "compose-dev-202"]
    assert [entry["topic"] for entry in contract["externally_produced_topics"]] == [
        _IN_TOPIC
    ]
    # Nothing renders the table yet; a served exposure with no reader turns
    # exposure-reader-coverage red.
    assert "projection_api" not in contract


def test_the_contract_routes_to_the_writer() -> None:
    routing = _contract()["handler_routing"]["handlers"]
    names = {entry["handler"]["name"] for entry in routing}
    assert names == {"HandlerContainerMemoryFold", "LabContainerMemoryProjectionWriter"}


def test_the_migration_creates_every_column_the_writer_inserts() -> None:
    sql = (
        _NODE_DIR / "migrations" / "0000_create_lab_container_memory_window.sql"
    ).read_text(encoding="utf-8")
    assert f"CREATE TABLE IF NOT EXISTS {TABLE}" in sql
    for column in (
        "record_key",
        "host",
        "host_boot_id",
        "lane",
        "container_id",
        "container_name",
        "container_started_at",
        "limit_bytes",
        "peak_bytes",
        "peak_window_start",
        "peak_window_end",
        "window_start",
        "window_end",
        "max_total",
        "max_delta",
        "oom_kill_total",
        "oom_kill_delta",
        "ci_runs",
        "projected_at",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column} " in sql, column
    assert "PRIMARY KEY (record_key)" in sql


# ---------------------------------------------------------------------------
# The runtime's own discovery and lane filter, run over the real contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lane", "attached"),
    [
        ("compose-dev-202", True),
        ("compose-dev", True),
        ("stability-test", False),
        ("sim-202", False),
    ],
)
def test_the_runtime_attaches_the_node_on_the_two_dev_lanes_only(
    lane: str, attached: bool
) -> None:
    """AC4's premise: discovery accepts the contract and the lane filter admits
    it on compose-dev-202 (and compose-dev), and on no other lane."""
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.profile_ownership import (
        filter_manifest_for_runtime_profile,
    )

    manifest = discover_contracts_from_paths([_NODE_DIR / "contract.yaml"])
    assert list(manifest.errors) == []
    result = filter_manifest_for_runtime_profile(
        manifest, "main", environ={"ONEX_RUNTIME_LANE": lane}
    )
    names = {contract.name for contract in result.manifest.contracts}
    assert ("projection_lab_container_memory" in names) is attached
    assert list(result.manifest.errors) == []
    if not attached:
        assert result.lane_excluded_contracts == ("projection_lab_container_memory",)


def test_a_runtime_that_names_no_lane_records_a_discovery_error() -> None:
    """Fail closed: a runtime with no declared lane never silently skips it."""
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.profile_ownership import (
        filter_manifest_for_runtime_profile,
    )

    manifest = discover_contracts_from_paths([_NODE_DIR / "contract.yaml"])
    result = filter_manifest_for_runtime_profile(manifest, "main", environ={})
    assert result.manifest.contracts == ()
    assert any(
        "projection_lab_container_memory" in str(error)
        for error in result.manifest.errors
    )


def test_the_writer_entry_works_from_inside_a_running_loop(
    writer: LabContainerMemoryProjectionWriter,
) -> None:
    """The synchronous entry must not assume the caller runs no event loop."""
    adapter: _KeyedTableAdapter = writer._db  # type: ignore[assignment]
    event = _event()

    async def _call_from_a_loop() -> dict[str, Any]:
        return writer.handle(_message(event, 12))

    applied = asyncio.run(_call_from_a_loop())
    assert applied["rows_upserted"] == 2
    assert len(adapter.rows) == 2
