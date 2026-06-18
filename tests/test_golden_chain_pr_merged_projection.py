# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain / data-flow tests for node_pr_merged_projection (OMN-13227 / T3).

Proves the full event -> projection materialization path with a synthetic
onex.evt.github.pr-merged.v1 event (no live publisher, no live broker):

  synthetic ModelPrMergedEvent
    -> HandlerPrMergedProjection.handle/project
      -> UPSERT into pr_merged_events (deduped by event_id)
        -> row carries {repo, branch, pr_number, ticket, merged_at}

and verifies the contract declares the read surface the worktree reaper (T4)
depends on: subscribe topic, projection_api exposure on
onex.evt.github.pr-merged.v1 with a monotonic cursor_column, and a node-local
migration that creates the table.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.events.github import ModelPrMergedEvent
from omnimarket.nodes.node_pr_merged_projection.handlers.handler_pr_merged_projection import (
    TABLE,
    HandlerPrMergedProjection,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

NODE_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_merged_projection"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"
MIGRATION_PATH = NODE_DIR / "migrations" / "0001_create_pr_merged_events.sql"

EXPECTED_SUBSCRIBE_TOPIC = "onex.evt.github.pr-merged.v1"

HANDLER = HandlerPrMergedProjection()


def _make_event(
    *,
    event_id: str = "evt-0001",
    repo: str = "OmniNode-ai/omnimarket",
    branch: str = "feature/omn-13227-projection",
    pr_number: int = 1265,
    ticket: str = "OMN-13227",
    merged_at: str = "2026-06-18T12:00:00+00:00",
) -> ModelPrMergedEvent:
    return ModelPrMergedEvent(
        event_id=event_id,
        repo=repo,
        branch=branch,
        pr_number=pr_number,
        ticket=ticket,
        merged_at=merged_at,
    )


@pytest.mark.unit
class TestPrMergedProjectionDataFlow:
    """event -> projection materialization with a synthetic pr-merged event."""

    def test_project_materializes_row(self) -> None:
        """A synthetic event lands one row carrying the reaper-match fields."""
        db = InmemoryDatabaseAdapter()
        result = HANDLER.project(_make_event(), db)
        assert result.rows_upserted == 1
        rows = db.query(TABLE)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_id"] == "evt-0001"
        assert row["repo"] == "OmniNode-ai/omnimarket"
        assert row["branch"] == "feature/omn-13227-projection"
        assert row["pr_number"] == 1265
        assert row["ticket"] == "OMN-13227"
        assert row["merged_at"] == "2026-06-18T12:00:00+00:00"
        # projection_cursor is DB-assigned (BIGSERIAL) — the handler must NOT
        # write it, or the cursor would not be monotonic across inserts.
        assert "projection_cursor" not in row

    def test_dedup_by_event_id_is_idempotent(self) -> None:
        """Projecting the same event_id twice leaves exactly one row."""
        db = InmemoryDatabaseAdapter()
        HANDLER.project(_make_event(event_id="evt-dup"), db)
        HANDLER.project(_make_event(event_id="evt-dup"), db)
        assert len(db.query(TABLE)) == 1

    def test_distinct_events_produce_distinct_rows(self) -> None:
        """Two merged PRs (distinct event_id) produce two rows."""
        db = InmemoryDatabaseAdapter()
        HANDLER.project(_make_event(event_id="evt-a", pr_number=1), db)
        HANDLER.project(_make_event(event_id="evt-b", pr_number=2), db)
        rows = db.query(TABLE)
        assert len(rows) == 2
        assert {r["pr_number"] for r in rows} == {1, 2}

    def test_handle_runtime_shim_requires_db_adapter(self) -> None:
        """handle() fails fast when no DatabaseAdapter is injected."""
        with pytest.raises(TypeError, match="DatabaseAdapter"):
            HANDLER.handle({"event_id": "x", "repo": "r", "branch": "b"})

    def test_handle_runtime_shim_projects_payload(self) -> None:
        """handle() coerces the publisher payload and projects it."""
        db = InmemoryDatabaseAdapter()
        out = HANDLER.handle(
            {
                "_db": db,
                "_event_type": EXPECTED_SUBSCRIBE_TOPIC,
                "event_id": "evt-shim",
                "topic": EXPECTED_SUBSCRIBE_TOPIC,
                "repo": "OmniNode-ai/omnimarket",
                "branch": "feature",
                "pr_number": 99,
                "ticket": "OMN-13227",
                "merged_at": "2026-06-18T12:00:00+00:00",
                "published_at": "2026-06-18T12:00:01+00:00",
            }
        )
        assert out["rows_upserted"] == 1
        assert len(db.query(TABLE)) == 1


@pytest.mark.unit
class TestPrMergedProjectionContract:
    """Contract declares the read surface the worktree reaper depends on."""

    def _load_contract(self) -> dict[str, object]:
        return yaml.safe_load(CONTRACT_PATH.read_text())  # type: ignore[return-value]

    def test_contract_and_migration_exist(self) -> None:
        assert CONTRACT_PATH.exists(), f"Missing contract at {CONTRACT_PATH}"
        assert MIGRATION_PATH.exists(), f"Missing migration at {MIGRATION_PATH}"

    def test_migration_creates_table(self) -> None:
        ddl = MIGRATION_PATH.read_text()
        assert "CREATE TABLE IF NOT EXISTS pr_merged_events" in ddl
        assert "projection_cursor BIGSERIAL" in ddl

    def test_node_type_is_reducer(self) -> None:
        assert self._load_contract().get("node_type") == "reducer"

    def test_subscribe_topic_is_canonical(self) -> None:
        event_bus = self._load_contract().get("event_bus") or {}
        assert isinstance(event_bus, dict)
        assert EXPECTED_SUBSCRIBE_TOPIC in (event_bus.get("subscribe_topics") or [])

    def test_projection_api_exposes_read_topic_with_cursor(self) -> None:
        """The generic projection API read path + monotonic cursor are declared."""
        section = self._load_contract().get("projection_api") or {}
        assert isinstance(section, dict)
        assert section.get("expose") is True
        exposures = section.get("exposures") or []
        match = next(
            (e for e in exposures if e.get("topic") == EXPECTED_SUBSCRIBE_TOPIC),
            None,
        )
        assert match is not None, (
            f"No projection_api exposure for {EXPECTED_SUBSCRIBE_TOPIC}"
        )
        assert match.get("table") == "pr_merged_events"
        assert match.get("cursor_column") == "projection_cursor"
        # Reaper-match fields must be exposed so T4 can match a merged PR to a
        # worktree without a second query.
        cols = set(match.get("columns") or [])
        assert {"repo", "branch", "pr_number", "ticket", "merged_at"} <= cols

    def test_db_io_declares_table(self) -> None:
        db_io = self._load_contract().get("db_io") or {}
        assert isinstance(db_io, dict)
        tables = {t.get("name") for t in (db_io.get("db_tables") or [])}
        assert "pr_merged_events" in tables
