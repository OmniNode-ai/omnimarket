# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (OMN-23865): the projection keeps the newest reading per host and class, with its read time."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.events.host_disk_reading import (
    ALL_CLASSES,
)
from omnimarket.nodes.node_projection_host_disk.handlers.handler_host_disk_writer import (
    ROWS_REFUSED_KEY,
    ROWS_UPSERTED_KEY,
    HostDiskProjectionWriter,
)
from omnimarket.nodes.node_projection_host_disk.handlers.handler_projection_host_disk import (
    HandlerProjectionHostDisk,
)
from tests.nodes.node_lab_disk_watermark_effect.host_disk_inputs import FAILED_TOPIC

from .host_disk_events import GIB, T0, TOPIC, StandInDatabase, later, reading

pytestmark = pytest.mark.unit
NODE = Path(HostDiskProjectionWriter.__module__.replace(".", "/")).parents[1]
CONTRACT = Path(
    __import__("omnimarket.nodes.node_projection_host_disk", fromlist=["x"]).__file__
).with_name("contract.yaml")


def _apply(writer: HostDiskProjectionWriter, db: Any, event: Any) -> dict[str, Any]:
    data = json.loads(event.model_dump_json())
    data["_meta"] = {"injected": "by the runtime"}
    return asyncio.run(writer._project_event(TOPIC, data, db))


def test_the_fold_makes_one_row_per_class_and_volume_and_is_deterministic() -> None:
    event = reading()
    first = HandlerProjectionHostDisk().handle(event)
    assert first == HandlerProjectionHostDisk().handle(event)
    assert [row.class_name for row in first.class_rows] == list(ALL_CLASSES)
    cache = first.class_rows[0]
    assert (cache.host_name, cache.status, cache.entry_count, cache.revision_count) == (
        "h202",
        "read",
        670,
        1,
    )
    assert cache.read_at == T0
    assert cache.groups[0]["package"] == "omnimarket"
    assert cache.groups[0]["copies"] == 670
    assert {row.status for row in first.class_rows[1:]} == {"unread"}
    assert [row.volume_path for row in first.volume_rows] == ["/"]


def test_an_unread_class_keeps_its_reason_and_no_size() -> None:
    row = HandlerProjectionHostDisk().handle(reading()).class_rows[1]
    assert row.unread_reason == "class root not declared in the overlay"
    assert (row.total_bytes, row.groups) == (0, ())


def test_the_first_reading_writes_every_row() -> None:
    db, writer = StandInDatabase(), HostDiskProjectionWriter()
    outcome = _apply(writer, db, reading())
    assert outcome[ROWS_UPSERTED_KEY] == len(ALL_CLASSES) + 1
    assert outcome[ROWS_REFUSED_KEY] == 0
    assert len(db.classes) == len(ALL_CLASSES)
    assert db.classes[("h202", "uv_cache")]["read_at"] == T0


def test_a_newer_reading_replaces_the_row_and_counts_it() -> None:
    db, writer = StandInDatabase(), HostDiskProjectionWriter()
    _apply(writer, db, reading())
    _apply(writer, db, reading(at=later(5), cache_copies=100, correlation_id="corr-b"))
    row = db.classes[("h202", "uv_cache")]
    assert row["read_at"] == later(5)
    assert row["reading_count"] == 2
    assert row["params"][5] == 100  # entry_count of the newer reading


def test_an_older_reading_never_replaces_a_newer_row() -> None:
    db, writer = StandInDatabase(), HostDiskProjectionWriter()
    _apply(writer, db, reading(at=later(5), cache_copies=100))
    outcome = _apply(
        writer, db, reading(at=T0, cache_copies=670, correlation_id="corr-old")
    )
    assert outcome[ROWS_UPSERTED_KEY] == 0
    assert outcome[ROWS_REFUSED_KEY] == len(ALL_CLASSES) + 1
    assert db.classes[("h202", "uv_cache")]["params"][5] == 100


def test_a_redelivery_writes_nothing() -> None:
    db, writer = StandInDatabase(), HostDiskProjectionWriter()
    _apply(writer, db, reading())
    outcome = _apply(writer, db, reading())
    assert outcome[ROWS_UPSERTED_KEY] == 0
    assert db.classes[("h202", "uv_cache")]["reading_count"] == 1


def test_two_hosts_keep_their_own_rows() -> None:
    db, writer = StandInDatabase(), HostDiskProjectionWriter()
    _apply(writer, db, reading(host="h202"))
    _apply(writer, db, reading(host="h053", cache_copies=3))
    assert {key[0] for key in db.classes} == {"h202", "h053"}
    assert db.classes[("h053", "uv_cache")]["params"][5] == 3


def test_the_writer_refuses_a_topic_it_does_not_consume() -> None:
    with pytest.raises(ValueError, match="does not consume topic"):
        HostDiskProjectionWriter().build_request(FAILED_TOPIC, {})


def test_the_writer_refuses_a_malformed_reading() -> None:
    with pytest.raises(ValidationError):
        HostDiskProjectionWriter().build_request(TOPIC, {"host_name": "h202"})


def test_the_writer_declares_in_process_dispatch_and_only_it_is_routed() -> None:
    assert HostDiskProjectionWriter.onex_runtime_inprocess_dispatch is True
    contract = yaml.safe_load(CONTRACT.read_text())
    operations = [
        entry["operation"] for entry in contract["handler_routing"]["handlers"]
    ]
    assert operations == ["host_disk_projection_writer"]


def test_the_contract_subscribes_to_the_reading_and_publishes_the_applied_event() -> (
    None
):
    contract = yaml.safe_load(CONTRACT.read_text())
    watermark = yaml.safe_load(
        (
            CONTRACT.parents[1] / "node_lab_disk_watermark_effect" / "contract.yaml"
        ).read_text()
    )
    assert contract["event_bus"]["subscribe_topics"] == [TOPIC]
    assert TOPIC in watermark["event_bus"]["publish_topics"]
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert contract["db_io"]["ordering_key"] == "read_at"
    assert {table["name"] for table in contract["db_io"]["db_tables"]} == {
        "host_disk_class",
        "host_disk_volume",
    }


def test_the_upsert_carries_the_stale_write_guard_in_sql() -> None:
    from omnimarket.nodes.node_projection_host_disk.handlers import (
        handler_host_disk_writer as writer,
    )

    for statement in (writer._UPSERT_CLASS, writer._UPSERT_VOLUME):
        assert "WHERE" in statement
        assert ".read_at < EXCLUDED.read_at" in statement


def test_the_migration_keys_and_grants_match_the_writer() -> None:
    migrations = CONTRACT.parent / "migrations"
    create = (migrations / "0000_create_host_disk.sql").read_text()
    grant = (migrations / "0001_grant_omninode_runtime_host_disk.sql").read_text()
    assert "PRIMARY KEY (host_name, class_name)" in create
    assert "PRIMARY KEY (host_name, volume_path)" in create
    assert "GRANT SELECT, INSERT, UPDATE" in grant
    statements = "\n".join(
        line for line in grant.splitlines() if not line.strip().startswith("--")
    )
    assert "DELETE" not in statements
    assert GIB  # the events module is the seam: its reading is what the writer upserts
