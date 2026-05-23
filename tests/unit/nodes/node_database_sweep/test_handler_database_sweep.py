# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_database_sweep handler — per-table staleness and UNKNOWN state."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep import (
    DatabaseSweepRequest,
    NodeDatabaseSweep,
    _check_table,
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
