# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded install-confirm output through the runtime-facing receipts writer."""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.models.context_install_confirm import (
    ModelInstallConfirmRequest,
)
from omnimarket.nodes.node_context_install_confirm_compute.handlers.handler_context_install_confirm import (
    HandlerContextInstallConfirm,
)
from omnimarket.nodes.node_projection_compaction_receipts import (
    NodeProjectionCompactionReceipts,
)
from omnimarket.nodes.node_projection_compaction_receipts.handlers.handler_compaction_receipts_writer import (
    _COLUMNS,
    _SELECT_PRIOR,
    _UPSERT,
    CompactionReceiptsProjectionWriter,
)
from omnimarket.nodes.node_projection_compaction_receipts.handlers.handler_projection_compaction_receipts import (
    HandlerProjectionCompactionReceipts,
)
from omnimarket.nodes.node_projection_compaction_receipts.models import (
    ModelCompactionReceipt,
    ModelCompactionReceiptsProjectionRequest,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.error_classification import (
    ProjectionErrorClass,
    classify_projection_error,
)
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import MessageMeta, PublishFn

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
NODE = ROOT / "src/omnimarket/nodes/node_projection_compaction_receipts"
FIXTURES = ROOT / "tests/fixtures/compaction_receipts"


def _recorded() -> dict[str, Any]:
    return json.loads((FIXTURES / "install_confirm_completed.json").read_text())


def _fold(
    payload: dict[str, Any], previous: tuple[ModelCompactionReceipt, ...] = ()
) -> tuple[ModelCompactionReceipt, ...]:
    return (
        HandlerProjectionCompactionReceipts()
        .handle(
            ModelCompactionReceiptsProjectionRequest.model_validate(
                {"event": payload, "previous_rows": previous}
            )
        )
        .rows
    )


class _Db:
    """Record actual SQL parameters; RETURNING, not attempted inserts, proves a write."""

    def __init__(
        self, prior: tuple[ModelCompactionReceipt, ...] = (), *, accepts: bool = True
    ) -> None:
        self.prior = prior
        self.accepts = accepts
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if query == _SELECT_PRIOR:
            return [row.model_dump() for row in self.prior]
        assert query == _UPSERT
        if not self.accepts:
            return []
        return [
            {
                **dict(zip(_COLUMNS, params[: len(_COLUMNS)], strict=True)),
                "source_topic": params[-1],
                "projected_at": datetime(2026, 10, 24, tzinfo=UTC),
                "projection_cursor": len(self.calls),
            }
        ]


@pytest.fixture(autouse=True)
def _binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")
    monkeypatch.delenv("OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY", raising=False)


class _RecordingWriter(CompactionReceiptsProjectionWriter):
    def __init__(self, db: _Db) -> None:
        super().__init__()
        self._db = cast(AsyncpgAdapter, db)
        self.snapshots: list[tuple[tuple[ProjectionTableConfig], dict[str, Any]]] = []

    async def _stop_producer(self) -> None:
        return None

    async def get_publish_fn(self) -> PublishFn | None:
        return None

    async def publish_snapshot_delta(
        self,
        exposure: ProjectionTableConfig,
        *,
        op: Literal["upsert", "delete"],
        row: dict[str, Any] | None,
        source_event_id: str,
        source_topic: str,
        source_partition: int,
        source_offset: int,
        tenant_id: str = "omninode",
        key: dict[str, Any] | None = None,
    ) -> bool:
        self.snapshots.append(
            (
                (exposure,),
                {
                    "op": op,
                    "row": row,
                    "source_event_id": source_event_id,
                    "source_topic": source_topic,
                    "source_partition": source_partition,
                    "source_offset": source_offset,
                    "tenant_id": tenant_id,
                    "key": key,
                },
            )
        )
        return True


def _writer(db: _Db) -> _RecordingWriter:
    return _RecordingWriter(db)


def test_recorded_payload_is_the_merged_compute_output() -> None:
    request = ModelInstallConfirmRequest.model_validate_json(
        (FIXTURES / "install_confirm_requested.json").read_text()
    )
    result = HandlerContextInstallConfirm().handle(request)
    assert result.model_dump(mode="json") == _recorded()


@pytest.mark.parametrize(
    ("boundary_index", "verdict", "reason"),
    [
        (0, "installed", None),
        (1, "install_mismatch", None),
        (2, "not_installed", "native_compaction"),
        (3, "uncertain", "unparseable_record"),
    ],
)
def test_recorded_payload_per_verdict(
    boundary_index: int, verdict: str, reason: str | None
) -> None:
    raw = _recorded()
    boundary = raw["boundaries"][boundary_index]
    db = _Db()
    out = _writer(db).handle(raw)
    assert out["rows_upserted"] == 5
    assert db.connected
    assert db.closed
    bound = next(
        params
        for query, params in db.calls
        if query == _UPSERT and params[2] == boundary["boundary_uuid"]
    )
    assert bound[:4] == (
        raw["session_id"],
        f"boundary:{boundary['boundary_uuid']}",
        boundary["boundary_uuid"],
        boundary["kind"],
    )
    assert (bound[5], bound[6], bound[8]) == (
        verdict,
        reason,
        boundary["observed_hash"],
    )
    row = next(
        r for r in out["compaction_receipts_rows"] if r["boundary_uuid"] == bound[2]
    )
    assert row["verdict"] == verdict
    assert row["reason"] == reason
    assert row["session_id"] == raw["session_id"]
    if boundary_index < 2:
        assert str(bound[4]) == boundary["activation_id"]
        assert bound[7] == raw["facts"][boundary_index]["installed_messages_hash"]


def test_capture_gap_is_recorded_without_inventing_a_boundary() -> None:
    db = _Db()
    out = _writer(db).handle(_recorded())
    gap = next(
        row for row in out["compaction_receipts_rows"] if row["boundary_uuid"] is None
    )
    assert gap["verdict"] == "uncertain"
    assert gap["reason"] == "capture_gap"
    assert gap["receipt_key"] == f"activation:{gap['activation_id']}"
    assert gap["boundary_kind"] is None


def test_not_selected_never_overwrites_the_selected_boundary() -> None:
    raw = _recorded()
    raw["facts"].reverse()
    rows = _fold(raw)
    first = next(
        row
        for row in rows
        if row.boundary_uuid == raw["boundaries"][0]["boundary_uuid"]
    )
    assert first.verdict == "installed"
    assert str(first.activation_id) == raw["boundaries"][0]["activation_id"]
    assert len([row for row in rows if row.boundary_uuid is not None]) == len(
        raw["boundaries"]
    )


def test_pending_activation_is_not_an_installation_receipt() -> None:
    raw = _recorded()
    raw.update(boundaries=[], facts=[], current={})
    db = _Db()
    assert _writer(db).handle(raw)["rows_upserted"] == 0
    assert [query for query, _ in db.calls] == [_SELECT_PRIOR]


def test_native_compaction_without_any_activation_still_gets_a_receipt() -> None:
    raw = _recorded()
    raw.update(boundaries=[raw["boundaries"][2]], facts=[], current={})
    (row,) = _fold(raw)
    assert row.activation_id is None
    assert (row.verdict, row.reason) == ("not_installed", "native_compaction")


@pytest.mark.parametrize(
    "damage",
    [
        "extra",
        "missing",
        "unknown_verdict",
        "empty_session",
        "hash",
        "false_installed",
        "absent_boundary",
        "duplicate_fact",
        "conflicting_boundary",
    ],
)
def test_malformed_event_is_refused_before_database_io(damage: str) -> None:
    raw = _recorded()
    if damage == "extra":
        raw["unexpected"] = "refuse"
    elif damage == "missing":
        del raw["facts"]
    elif damage == "unknown_verdict":
        raw["facts"][-1]["fact"] = "success"
    elif damage == "empty_session":
        raw["session_id"] = ""
    elif damage == "hash":
        raw["facts"][0]["installed_messages_hash"] = "not-a-hash"
    elif damage == "false_installed":
        raw["facts"][0]["installed_messages_hash"] = "0" * 64
    elif damage == "absent_boundary":
        raw["facts"][0]["boundary_uuid"] = "uncaptured"
    elif damage == "duplicate_fact":
        raw["facts"].append(raw["facts"][0])
    else:
        raw["boundaries"].append({**raw["boundaries"][0], "observed_hash": "0" * 64})
    db = _Db()
    with pytest.raises(ValidationError) as failure:
        _writer(db).handle(raw)
    assert classify_projection_error(failure.value) is ProjectionErrorClass.POISON
    assert not db.connected
    assert db.calls == []


def test_redelivery_and_out_of_order_uncertainty_cannot_replace_decided_rows() -> None:
    raw = _recorded()
    previous = _fold(raw)
    db = _Db(previous)
    assert _writer(db).handle(raw)["rows_upserted"] == 0
    assert [query for query, _ in db.calls] == [_SELECT_PRIOR]
    # A later delivery with a partial capture of the same boundary is weaker.
    raw.update(
        boundaries=[
            {
                **raw["boundaries"][0],
                "kind": "unreadable",
                "activation_id": None,
                "observed_hash": None,
            }
        ],
        facts=[],
        current={},
    )
    assert _fold(raw, previous) == ()


def test_fuller_capture_resolves_uncertainty_without_creating_another_boundary() -> (
    None
):
    raw = _recorded()
    raw.update(boundaries=raw["boundaries"][:1], facts=raw["facts"][:1], current={})
    partial = {
        **raw,
        "boundaries": [
            {
                **raw["boundaries"][0],
                "kind": "unreadable",
                "activation_id": None,
                "observed_hash": None,
            }
        ],
        "facts": [],
    }
    (uncertain,) = _fold(partial)
    db = _Db((uncertain,))
    out = _writer(db).handle(raw)
    assert out["rows_upserted"] == 1
    assert out["compaction_receipts_rows"][0]["verdict"] == "installed"
    assert out["compaction_receipts_rows"][0]["receipt_key"] == uncertain.receipt_key


def test_fold_is_deterministic_under_reordered_captures() -> None:
    raw = _recorded()
    expected = _fold(raw)
    rng = random.Random(21336)
    for _ in range(10):
        rng.shuffle(raw["boundaries"])
        rng.shuffle(raw["facts"])
        assert _fold(raw) == expected


def test_sql_guard_covers_a_racing_delivery_and_advances_the_read_cursor() -> None:
    assert "ON CONFLICT (session_id, receipt_key)" in _UPSERT
    assert (
        "WHERE omninode_internal.compaction_receipts.verdict = 'uncertain' AND EXCLUDED.verdict <> 'uncertain'"
        in _UPSERT
    )
    assert (
        "projection_cursor = nextval('omninode_internal.compaction_receipts_projection_cursor_seq')"
        in _UPSERT
    )
    db = _Db(accepts=False)
    writer = _writer(db)
    assert writer.handle(_recorded())["rows_upserted"] == 0
    assert writer.snapshots == []


def test_runner_envelope_keys_are_stripped_and_coordinates_reach_the_snapshot() -> None:
    raw = _recorded()
    delivered = unwrap_envelope(
        json.dumps({"data": raw, "event_type": "completed"}).encode()
    )
    assert delivered is not None
    delivered.update(_db=object(), _envelope_id="fixture", _partition=2, _offset=17)
    before = dict(delivered)
    writer = _writer(_Db())
    assert writer.handle(delivered)["rows_upserted"] == 5
    assert delivered == before
    snapshots = writer.snapshots
    assert len(snapshots) == 5
    assert all(
        kwargs["source_partition"] == 2 and kwargs["source_offset"] == 17
        for _, kwargs in snapshots
    )
    assert {kwargs["row"]["verdict"] for _, kwargs in snapshots} == {
        "installed",
        "install_mismatch",
        "not_installed",
        "uncertain",
    }


async def test_standalone_runner_uses_the_same_fold_and_write() -> None:
    writer = _writer(_Db())
    topic = writer.subscribe_topics[0]
    assert await writer.project_event(
        topic, _recorded(), MessageMeta(1, 19, "fixture", topic)
    )
    assert len(writer.snapshots) == 5


def test_consumer_contract_and_read_exposure() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    producer = yaml.safe_load(
        (
            ROOT
            / "src/omnimarket/nodes/node_context_install_confirm_compute/contract.yaml"
        ).read_text()
    )
    assert contract["event_bus"]["subscribe_topics"] == [producer["terminal_event"]]
    assert [
        entry["handler"]["name"] for entry in contract["handler_routing"]["handlers"]
    ] == ["CompactionReceiptsProjectionWriter"]
    assert CompactionReceiptsProjectionWriter.onex_runtime_inprocess_dispatch is True
    writer = CompactionReceiptsProjectionWriter()
    assert writer.poison_dlq_topics == contract["event_bus"]["dlq_topics"]
    (exposure,) = build_projection_topic_map(
        SimpleNamespace(
            contracts=[
                SimpleNamespace(
                    name=contract["name"], contract_path=NODE / "contract.yaml"
                )
            ]
        )
    ).values()
    assert exposure.bus_backed
    assert exposure.key_columns == ("session_id", "receipt_key")
    assert exposure.table == "compaction_receipts"
    assert exposure.relation_schema == "omninode_internal"
    assert {"session_id", "boundary_uuid", "verdict", "projection_cursor"} <= set(
        exposure.columns
    )
    assert exposure.cursor_column == "projection_cursor"
    assert exposure.topic in contract["event_bus"]["publish_topics"]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert (
        project["project"]["entry-points"]["onex.nodes"][NODE.name]
        == f"omnimarket.nodes.{NODE.name}"
    )
    assert (
        NodeProjectionCompactionReceipts.__name__ == "NodeProjectionCompactionReceipts"
    )


async def test_malformed_standalone_event_is_refused_without_writes() -> None:
    db = _Db()
    writer = _writer(db)
    with pytest.raises(ValidationError):
        await writer.project_event(
            writer.subscribe_topics[0],
            {"session_id": "malformed"},
            MessageMeta(0, 1, "fixture"),
        )
    assert db.calls == []


async def test_malformed_quarantine_requires_a_publisher() -> None:
    writer = _writer(_Db())

    with pytest.raises(RuntimeError, match="quarantine"):
        await writer.publish_dlq(writer.poison_dlq_topics[0], b"malformed")


async def test_projection_read_returns_installed_with_boundary_uuid(
    tmp_path: Path,
) -> None:
    """Exercise the shipped exposure and read node over returned rows in a local store.

    This hosted preparation probe is independent of a live /compact and deploy.
    """
    from omnimarket.models.model_projection_read import ModelProjectionReadRequest
    from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
        HandlerProjectionRead,
    )
    from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
        SqliteTableRowSource,
    )

    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    topic_map = build_projection_topic_map(
        SimpleNamespace(
            contracts=[
                SimpleNamespace(
                    name=contract["name"], contract_path=NODE / "contract.yaml"
                )
            ]
        )
    )
    (exposure,) = topic_map.values()
    returned = (await asyncio.to_thread(_writer(_Db()).handle, _recorded()))[
        "compaction_receipts_rows"
    ]
    store = tmp_path / "receipts.sqlite"
    with sqlite3.connect(store) as conn:
        conn.execute(
            f"CREATE TABLE compaction_receipts ({', '.join(column + (' INTEGER' if column == 'projection_cursor' else ' TEXT') for column in exposure.columns)})"
        )
        for row in returned:
            conn.execute(
                f"INSERT INTO compaction_receipts VALUES ({', '.join('?' for _ in exposure.columns)})",
                tuple(row[column] for column in exposure.columns),
            )
    reader = HandlerProjectionRead(
        topic_map=topic_map, row_source=SqliteTableRowSource(store)
    )
    result = await reader.handle(ModelProjectionReadRequest(topic=exposure.topic))
    assert result.ok, result
    assert result.row_count == 5
    (installed,) = [row for row in result.rows if row["verdict"] == "installed"]
    raw = _recorded()
    assert installed["session_id"] == raw["session_id"]
    assert installed["boundary_uuid"] == raw["boundaries"][0]["boundary_uuid"]
    assert installed["activation_id"] == raw["boundaries"][0]["activation_id"]


def test_bus_backed_exposure_declares_a_reasoned_reader_opt_out() -> None:
    """exposure-reader-coverage (OMN-17199) refuses a bus_backed exposure with no reader."""
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    (exposure,) = contract["projection_api"]["exposures"]
    assert exposure["bus_backed"] is True
    assert exposure["consumers"] == "none"
    assert "node_projection_read_effect" in exposure["consumers_reason"]
