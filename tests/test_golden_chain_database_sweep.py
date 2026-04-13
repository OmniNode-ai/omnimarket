"""Golden chain tests for node_database_sweep.

Tests the handler logic with mocked psql and filesystem state.
No real database connection required.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep import (
    DatabaseSweepRequest,
    ModelMigrationStateResult,
    NodeDatabaseSweep,
    _check_alembic_migration,
    _check_drizzle_migration,
    _check_table,
)

CMD_TOPIC = "onex.cmd.omnimarket.database-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.database-sweep-completed.v1"


def _make_psql_ok(row_count: int, latest: str, status: str) -> tuple[int, str, str]:
    return (0, f"{row_count}|{latest}|{status}", "")


@pytest.mark.unit
class TestDatabaseSweepGoldenChain:
    """Golden chain: command -> handler -> completion event."""

    async def test_healthy_table(self, event_bus: EventBusInmemory) -> None:
        """A table with rows and fresh data should be HEALTHY."""
        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            # 1 call: column existence check (created_at found), 1 call: health query
            mock_psql.side_effect = [
                (0, "1", ""),  # has_ts_col check — created_at exists
                (0, "100|2026-04-09 12:00:00|HEALTHY", ""),  # health query
            ]
            result = _check_table(
                "test_table", "omnidash_analytics", 24, {"test_table"}
            )

        assert result.status == "HEALTHY"
        assert result.row_count == 100
        assert result.drizzle_defined is True

    async def test_empty_table(self, event_bus: EventBusInmemory) -> None:
        """A table with 0 rows should be EMPTY."""
        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            mock_psql.side_effect = [
                (0, "1", ""),  # created_at exists
                (0, "0||EMPTY", ""),  # health query
            ]
            result = _check_table("empty_table", "omnidash_analytics", 24, set())

        assert result.status == "EMPTY"
        assert result.row_count == 0
        assert result.drizzle_defined is False

    async def test_missing_table_on_query_error(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A table that errors on query should be classified as MISSING."""
        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            mock_psql.side_effect = [
                (0, "1", ""),  # created_at exists
                (1, "", "relation does not exist"),  # health query fails
            ]
            result = _check_table("ghost_table", "omnidash_analytics", 24, set())

        assert result.status == "MISSING"

    async def test_no_timestamp_column(self, event_bus: EventBusInmemory) -> None:
        """A table with no timestamp column should be classified as NO_TIMESTAMP or EMPTY."""
        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            # All column-check calls return no row (column not found) — 5 columns
            ts_cols = [
                "created_at",
                "timestamp",
                "emitted_at",
                "updated_at",
                "recorded_at",
            ]
            no_col_responses = [(0, "", "")] * len(ts_cols)
            # Final row count query
            no_col_responses += [(0, "42", "")]
            mock_psql.side_effect = no_col_responses
            result = _check_table("no_ts_table", "omnidash_analytics", 24, set())

        assert result.status == "NO_TIMESTAMP"
        assert result.row_count == 42

    async def test_alembic_current(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """An Alembic DB with a head version should be CURRENT."""
        # handler builds: omni_home / repo / versions_path
        versions_dir = tmp_path / "test_repo" / "migrations" / "versions"
        versions_dir.mkdir(parents=True)
        for i in range(3):
            (versions_dir / f"rev_{i:03d}.py").write_text(f"revision = '{i:03d}'\n")

        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            mock_psql.return_value = (0, "1|abc123def456", "")
            result = _check_alembic_migration(
                repo="test_repo",
                database="test_db",
                versions_path="migrations/versions",
                omni_home=str(tmp_path),
            )

        assert result.status == "CURRENT"
        assert result.disk_migrations == 3
        assert result.current_head == "abc123def456"

    async def test_drizzle_pending(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """A Drizzle DB with fewer applied than disk migrations should be PENDING."""
        # handler builds: omni_home / repo / migrations_path
        migrations_dir = tmp_path / "omnidash" / "migrations"
        migrations_dir.mkdir(parents=True)
        for i in range(5):
            (migrations_dir / f"migration_{i:04d}.sql").write_text("-- migration\n")

        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            mock_psql.return_value = (0, "3", "")
            result = _check_drizzle_migration(
                repo="omnidash",
                database="omnidash_analytics",
                migrations_path="migrations",
                omni_home=str(tmp_path),
            )

        assert result.status == "PENDING"
        assert result.disk_migrations == 5
        assert result.applied_migrations == 3

    async def test_drizzle_current(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """A Drizzle DB with all migrations applied should be CURRENT."""
        migrations_dir = tmp_path / "omnidash" / "migrations"
        migrations_dir.mkdir(parents=True)
        for i in range(3):
            (migrations_dir / f"migration_{i:04d}.sql").write_text("-- migration\n")

        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            mock_psql.return_value = (0, "3", "")
            result = _check_drizzle_migration(
                repo="omnidash",
                database="omnidash_analytics",
                migrations_path="migrations",
                omni_home=str(tmp_path),
            )

        assert result.status == "CURRENT"

    async def test_dry_run_propagates(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """dry_run flag should propagate to result."""
        with (
            patch(
                "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._get_all_tables"
            ) as mock_tables,
            patch(
                "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._get_drizzle_tables"
            ) as mock_drizzle,
            patch(
                "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._check_alembic_migration"
            ) as mock_alembic,
            patch(
                "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._check_drizzle_migration"
            ) as mock_drizzle_mig,
        ):
            mock_tables.return_value = []
            mock_drizzle.return_value = set()
            mock_alembic.return_value = None
            mock_drizzle_mig.return_value = None

            # Patch the internal migration check calls
            from omnimarket.nodes.node_database_sweep.handlers import (
                handler_database_sweep as hmod,
            )

            mock_alembic.return_value = ModelMigrationStateResult(
                database="test", repo="test", migration_tool="alembic", status="CURRENT"
            )
            mock_drizzle_mig.return_value = ModelMigrationStateResult(
                database="test", repo="test", migration_tool="drizzle", status="CURRENT"
            )

            handler = NodeDatabaseSweep()
            request = DatabaseSweepRequest(
                omni_home=str(tmp_path),
                dry_run=True,
            )

            with (
                patch.object(hmod, "_ALEMBIC_REPOS", []),
                patch.object(hmod, "_DRIZZLE_REPOS", []),
            ):
                result = handler.handle(request)

        assert result.dry_run is True

    async def test_event_bus_wiring(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """Handler can publish completion event to EventBusInmemory."""
        handler = NodeDatabaseSweep()
        events_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            with (
                patch(
                    "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._get_all_tables",
                    return_value=[],
                ),
                patch(
                    "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._get_drizzle_tables",
                    return_value=set(),
                ),
            ):
                from omnimarket.nodes.node_database_sweep.handlers import (
                    handler_database_sweep as hmod,
                )

                with (
                    patch.object(hmod, "_ALEMBIC_REPOS", []),
                    patch.object(hmod, "_DRIZZLE_REPOS", []),
                ):
                    request = DatabaseSweepRequest(
                        omni_home=payload.get("omni_home", str(tmp_path)),
                        dry_run=True,
                    )
                    result = handler.handle(request)

            evt = {"status": result.status}
            events_captured.append(evt)
            await event_bus.publish(EVT_TOPIC, key=None, value=json.dumps(evt).encode())

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-db-sweep"
        )

        cmd_payload = json.dumps({"omni_home": str(tmp_path)}).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(events_captured) == 1
        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()
