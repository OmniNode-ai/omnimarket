# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_database_sweep handler — per-table staleness and UNKNOWN state."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep import (
    DatabaseSweepRequest,
    NodeDatabaseSweep,
    _check_node_migrations,
    _check_table,
    _discover_node_migration_ids,
)


@pytest.mark.unit
class TestCheckTableFreshnessStates:
    """_check_table correctly classifies all freshness states."""

    def test_known_fresh(self) -> None:
        """Table within threshold and in Drizzle schema → HEALTHY."""
        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            mock_psql.side_effect = [
                (0, "1", ""),  # created_at column exists
                (0, "500|2026-05-23 00:00:00|HEALTHY", ""),  # health query
            ]
            result = _check_table(
                "node_service_registry",
                "omnidash_analytics",
                24,
                {"node_service_registry"},
            )

        assert result.status == "HEALTHY"
        assert result.row_count == 500
        assert result.drizzle_defined is True

    def test_known_stale(self) -> None:
        """Table exceeding threshold and in Drizzle schema → STALE."""
        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            mock_psql.side_effect = [
                (0, "1", ""),  # created_at column exists
                (0, "10|2026-05-01 00:00:00|STALE", ""),  # health query
            ]
            result = _check_table(
                "delegation_events",
                "omnidash_analytics",
                1,
                {"delegation_events"},
            )

        assert result.status == "STALE"
        assert result.row_count == 10
        assert result.drizzle_defined is True

    def test_unknown_no_drizzle_no_threshold(self) -> None:
        """Table not in Drizzle schema and no per-table threshold → UNKNOWN."""
        result = _check_table(
            "mystery_table",
            "omnidash_analytics",
            24,
            set(),  # not drizzle-defined
            staleness_thresholds=None,
        )

        assert result.status == "UNKNOWN"
        assert result.drizzle_defined is False
        assert "freshness metadata" in result.message

    def test_unknown_no_timestamp_column(self) -> None:
        """Drizzle-defined table with no timestamp column → UNKNOWN."""
        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            # All 5 timestamp column checks fail (column not present)
            mock_psql.side_effect = [(0, "", "")] * 5
            result = _check_table(
                "pattern_learning_artifacts",
                "omnidash_analytics",
                24,
                {"pattern_learning_artifacts"},
            )

        assert result.status == "UNKNOWN"
        assert "no timestamp column" in result.message

    def test_per_table_override(self) -> None:
        """Per-table threshold of 0.083h (5 min) is used instead of global 24h."""
        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            mock_psql.side_effect = [
                (0, "1", ""),  # created_at column exists
                (0, "200|2026-05-23 10:00:00|STALE", ""),  # stale at 5-min threshold
            ]
            result = _check_table(
                "node_service_registry",
                "omnidash_analytics",
                24,
                {"node_service_registry"},
                staleness_thresholds={"node_service_registry": 5 / 60},
            )

        assert result.status == "STALE"
        # Verify the override threshold was used in the SQL query
        call_args = mock_psql.call_args_list[1]
        query = call_args[0][0]
        assert "5 / 60" in query or str(5 / 60) in query or "0.08" in query

    def test_fallback_to_global(self) -> None:
        """Table without specific per-table threshold uses global default."""
        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            mock_psql.side_effect = [
                (0, "1", ""),  # created_at column exists
                (0, "50|2026-05-23 00:00:00|HEALTHY", ""),  # health query
            ]
            result = _check_table(
                "delegation_events",
                "omnidash_analytics",
                48,  # global threshold
                {"delegation_events"},
                staleness_thresholds={"some_other_table": 1.0},  # doesn't apply
            )

        assert result.status == "HEALTHY"
        call_args = mock_psql.call_args_list[1]
        query = call_args[0][0]
        assert "48" in query  # global threshold applied

    def test_invalid_per_table_threshold_is_unknown_without_sql(self) -> None:
        """Invalid per-table thresholds are rejected before freshness SQL."""
        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            result = _check_table(
                "node_service_registry",
                "omnidash_analytics",
                24,
                {"node_service_registry"},
                staleness_thresholds={"node_service_registry": -1.0},
            )

        assert result.status == "UNKNOWN"
        assert "invalid staleness threshold" in result.message
        mock_psql.assert_not_called()

    def test_unknown_not_reclassified_as_orphan(self) -> None:
        """UNKNOWN tables are not re-classified as ORPHAN by the handler."""
        handler = NodeDatabaseSweep()

        with (
            patch(
                "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._get_all_tables",
                return_value=["mystery_table"],
            ),
            patch(
                "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._get_drizzle_tables",
                return_value=set(),
            ),
            patch(
                "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._check_alembic_migration"
            ),
            patch(
                "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._check_drizzle_migration"
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
                    omni_home="/tmp",
                    staleness_thresholds=None,
                )
                result = handler.handle(request)

        table = next(
            (t for t in result.table_results if t.table_name == "mystery_table"), None
        )
        assert table is not None
        assert table.status == "UNKNOWN"
        assert result.tables_unknown == 1

    def test_tables_unknown_counted_in_result(self) -> None:
        """Handler aggregates tables_unknown count correctly."""
        handler = NodeDatabaseSweep()

        with (
            patch(
                "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._get_all_tables",
                return_value=["table_a", "table_b"],
            ),
            patch(
                "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._get_drizzle_tables",
                return_value=set(),  # neither table is drizzle-defined
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
                    omni_home="/tmp",
                    staleness_thresholds=None,
                )
                result = handler.handle(request)

        assert result.tables_unknown == 2
        assert result.tables_orphan == 0  # not re-classified as orphan


@pytest.mark.unit
class TestCheckTableEdgeCases:
    """Edge cases for _check_table."""

    def test_missing_table_query_error(self) -> None:
        """Query error on health check → MISSING."""
        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            mock_psql.side_effect = [
                (0, "1", ""),  # created_at exists
                (1, "", "relation does not exist"),  # health query fails
            ]
            result = _check_table(
                "ghost_table",
                "omnidash_analytics",
                24,
                {"ghost_table"},
            )

        assert result.status == "MISSING"

    def test_unknown_table_with_explicit_threshold_not_unknown(self) -> None:
        """Non-Drizzle table with explicit per-table threshold → proceeds normally (not UNKNOWN)."""
        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            mock_psql.side_effect = [
                (0, "1", ""),  # created_at column exists
                (0, "100|2026-05-23 10:00:00|HEALTHY", ""),
            ]
            result = _check_table(
                "node_service_registry",
                "omnidash_analytics",
                24,
                set(),  # not in Drizzle schema
                staleness_thresholds={"node_service_registry": 5 / 60},
            )

        assert result.status == "HEALTHY"
        assert result.drizzle_defined is False


@pytest.mark.unit
class TestNodeMigrationApplicationGap:
    """_check_node_migrations detects vendored node migrations that never applied.

    OMN-13636 (WS-F Phase 4): a node-owned migration file present on disk
    (vendored under src/omnimarket/nodes/<node>/migrations/*.sql, mirrored into
    omnibase_infra docker/migrations/forward/nodes/) but with no row in
    public.schema_migrations is the silent-skip failure mode that left
    delegation_events without context_pack_hash. The sweep must surface it as
    PENDING (a fail-loud detection path), never silently report CURRENT.
    """

    def test_discover_node_migration_ids_namespaces_filenames(
        self, tmp_path: Path
    ) -> None:
        """Discovery walks <node>/migrations/*.sql and namespaces each id."""
        node_a = tmp_path / "src" / "omnimarket" / "nodes" / "node_alpha" / "migrations"
        node_a.mkdir(parents=True)
        (node_a / "0001_one.sql").write_text("SELECT 1;")
        (node_a / "0002_two.sql").write_text("SELECT 2;")
        node_b = tmp_path / "src" / "omnimarket" / "nodes" / "node_beta" / "migrations"
        node_b.mkdir(parents=True)
        (node_b / "0001_b.sql").write_text("SELECT 3;")

        ids = _discover_node_migration_ids(str(tmp_path))

        assert ids == {
            "node:node_alpha:0001_one.sql",
            "node:node_alpha:0002_two.sql",
            "node:node_beta:0001_b.sql",
        }

    def test_discover_returns_empty_when_no_source_tree(self, tmp_path: Path) -> None:
        """No src/omnimarket/nodes tree → empty set (not an exception)."""
        assert _discover_node_migration_ids(str(tmp_path)) == set()

    def test_pending_when_disk_file_has_no_applied_row(self, tmp_path: Path) -> None:
        """A vendored migration with no schema_migrations row → PENDING (the gap)."""
        node_dir = (
            tmp_path
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_projection_delegation"
            / "migrations"
        )
        node_dir.mkdir(parents=True)
        (node_dir / "0019_delegation_budget_state.sql").write_text("SELECT 1;")
        (node_dir / "0020_delegation_context_pack_hash.sql").write_text("SELECT 2;")

        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            # schema_migrations exists; only 0019 was actually applied.
            mock_psql.return_value = (
                0,
                "node:node_projection_delegation:0019_delegation_budget_state.sql",
                "",
            )
            result = _check_node_migrations("omnidash_analytics", str(tmp_path))

        assert result.status == "PENDING"
        assert result.disk_migrations == 2
        assert result.applied_migrations == 1
        # The specific missing file must be named so triage is actionable.
        assert "0020_delegation_context_pack_hash.sql" in result.message

    def test_current_when_all_applied(self, tmp_path: Path) -> None:
        """Every vendored migration has an applied row → CURRENT."""
        node_dir = (
            tmp_path / "src" / "omnimarket" / "nodes" / "node_alpha" / "migrations"
        )
        node_dir.mkdir(parents=True)
        (node_dir / "0001_one.sql").write_text("SELECT 1;")

        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            mock_psql.return_value = (0, "node:node_alpha:0001_one.sql", "")
            result = _check_node_migrations("omnidash_analytics", str(tmp_path))

        assert result.status == "CURRENT"
        assert result.disk_migrations == 1
        assert result.applied_migrations == 1
        assert result.message == ""

    def test_no_table_when_schema_migrations_query_fails(self, tmp_path: Path) -> None:
        """schema_migrations table missing → NO_TABLE, not CURRENT."""
        node_dir = (
            tmp_path / "src" / "omnimarket" / "nodes" / "node_alpha" / "migrations"
        )
        node_dir.mkdir(parents=True)
        (node_dir / "0001_one.sql").write_text("SELECT 1;")

        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            mock_psql.return_value = (
                1,
                "",
                "relation schema_migrations does not exist",
            )
            result = _check_node_migrations("omnidash_analytics", str(tmp_path))

        assert result.status == "NO_TABLE"
        assert result.disk_migrations == 1

    def test_error_when_no_source_tree(self, tmp_path: Path) -> None:
        """No node migration source tree resolvable → ERROR (cannot verify)."""
        with patch(
            "omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep._psql"
        ) as mock_psql:
            result = _check_node_migrations("omnidash_analytics", str(tmp_path))

        assert result.status == "ERROR"
        mock_psql.assert_not_called()

    def test_handler_counts_pending_node_migration(self, tmp_path: Path) -> None:
        """Handler surfaces a node-migration PENDING in the aggregate result."""
        node_dir = (
            tmp_path / "src" / "omnimarket" / "nodes" / "node_alpha" / "migrations"
        )
        node_dir.mkdir(parents=True)
        (node_dir / "0001_one.sql").write_text("SELECT 1;")

        handler = NodeDatabaseSweep()

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
                patch.object(
                    hmod,
                    "_psql",
                    return_value=(
                        0,
                        "",
                        "",
                    ),  # schema_migrations empty: nothing applied
                ),
            ):
                request = DatabaseSweepRequest(omni_home=str(tmp_path))
                result = handler.handle(request)

        node_mig = next(
            (
                m
                for m in result.migration_results
                if m.migration_tool == "node-vendored"
            ),
            None,
        )
        assert node_mig is not None
        assert node_mig.status == "PENDING"
        assert result.migrations_pending >= 1
        assert result.status == "issues_found"

    @pytest.mark.parametrize("node_status", ["ERROR", "NO_TABLE"])
    def test_handler_fails_on_node_migration_verification_gap(
        self, tmp_path: Path, node_status: str
    ) -> None:
        """Handler treats unverifiable node migrations as issues_found."""
        handler = NodeDatabaseSweep()

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

            node_result = hmod.ModelMigrationStateResult(
                database="omnidash_analytics",
                repo="omnimarket",
                migration_tool="node-vendored",
                status=node_status,
                message="node migration verification unavailable",
            )
            with (
                patch.object(hmod, "_ALEMBIC_REPOS", []),
                patch.object(hmod, "_DRIZZLE_REPOS", []),
                patch.object(hmod, "_check_node_migrations", return_value=node_result),
            ):
                request = DatabaseSweepRequest(omni_home=str(tmp_path))
                result = handler.handle(request)

        assert result.status == "issues_found"
