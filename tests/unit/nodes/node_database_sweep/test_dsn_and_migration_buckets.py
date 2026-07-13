# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13717: database_sweep DSN alignment + migration bucket classification.

Reproduces the live failure where ``database_sweep`` shelled a bare
``psql -d <db>`` against the local unix socket (discovering 0 tables on the .201
lanes that have no local Postgres) and left every migration bucket at 0 even
though every database errored. The fix routes psql through the lane Postgres
container (mirroring node_data_flow_sweep) and classifies ERROR/NO_TABLE as
``failed``.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_database_sweep.handlers import handler_database_sweep as h
from omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep import (
    ModelMigrationStateResult,
    _build_psql_argv,
    _classify_migration_buckets,
    _get_all_tables,
)


@pytest.mark.unit
class TestPsqlConnectionTarget:
    """_build_psql_argv routes through the lane Postgres container, not a socket."""

    def test_remote_lane_uses_ssh_docker_exec(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured runtime host => ssh + docker exec into the PG container."""
        monkeypatch.setenv("ONEX_DATABASE_SWEEP_RUNTIME_HOST", "10.0.0.9")
        monkeypatch.setenv("ONEX_DATABASE_SWEEP_PG_CONTAINER", "lane-postgres")
        monkeypatch.setenv("ONEX_DATABASE_SWEEP_PG_USER", "postgres")
        monkeypatch.setenv("ONEX_DATABASE_SWEEP_SSH_USER", "deploy")

        argv = _build_psql_argv("SELECT 1;", "omnidash_analytics")

        assert argv[0] == "ssh"
        assert argv[1] == "deploy@10.0.0.9"
        # The remote command runs psql inside the container — never a bare
        # local-socket psql (the OMN-13717 bug).
        remote = argv[2]
        assert "docker exec lane-postgres psql" in remote
        assert "-d omnidash_analytics" in remote
        assert argv[0] != "psql"

    def test_local_lane_uses_docker_exec_no_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty runtime host => local docker exec, still never a bare psql."""
        monkeypatch.setenv("ONEX_DATABASE_SWEEP_RUNTIME_HOST", "")
        monkeypatch.setenv(
            "ONEX_DATABASE_SWEEP_PG_CONTAINER", "omnibase-infra-postgres"
        )

        argv = _build_psql_argv("SELECT 1;", "omnidash_analytics")

        assert argv[0] == "docker"
        assert argv[:3] == ["docker", "exec", "omnibase-infra-postgres"]
        assert "ssh" not in argv

    def test_table_discovery_with_injected_runner(self) -> None:
        """Given a working psql runner, table discovery returns the tables.

        Proves the discovery logic itself is correct once the connection
        reaches a real DB — the live failure was the connection target, not the
        parsing.
        """

        def fake_psql(query: str, database: str) -> tuple[int, str, str]:
            assert database == "omnidash_analytics"
            return 0, "agent_routing_decisions\nnode_service_registry\n", ""

        tables, scan_error = _get_all_tables("omnidash_analytics", fake_psql)

        assert tables == ["agent_routing_decisions", "node_service_registry"]
        assert scan_error == ""


@pytest.mark.unit
class TestMigrationBucketClassification:
    """_classify_migration_buckets folds failure states into the failed bucket."""

    @staticmethod
    def _mig(status: str) -> ModelMigrationStateResult:
        return ModelMigrationStateResult(
            database="omnidash_analytics",
            repo="omnimarket",
            migration_tool="node-vendored",
            status=status,
        )

    def test_error_and_no_table_count_as_failed(self) -> None:
        """The exact live-receipt mix (4x ERROR + 1x NO_TABLE) => failed=5."""
        results = [self._mig("ERROR") for _ in range(4)] + [self._mig("NO_TABLE")]

        current, pending, failed = _classify_migration_buckets(results)

        assert (current, pending, failed) == (0, 0, 5)

    def test_full_status_spread(self) -> None:
        """CURRENT->current, PENDING/AHEAD->pending, FAILED/NO_TABLE/ERROR->failed."""
        results = [
            self._mig("CURRENT"),
            self._mig("PENDING"),
            self._mig("AHEAD"),
            self._mig("FAILED"),
            self._mig("NO_TABLE"),
            self._mig("ERROR"),
        ]

        current, pending, failed = _classify_migration_buckets(results)

        assert (current, pending, failed) == (1, 2, 3)

    def test_default_psql_never_bare_local_socket(self) -> None:
        """The module default psql builds a container-routed argv, not bare psql.

        Guards against a regression to ``psql -d <db>`` (the local-socket DSN
        that produced 0 tables in the OMN-13717 receipt).
        """
        argv = h._build_psql_argv("SELECT 1;", "omnidash_analytics")
        assert argv[0] in ("ssh", "docker")
        assert argv[0] != "psql"


@pytest.mark.unit
class TestTableScanFailsClosed:
    """OMN-14526: a table scan that could not run must FAIL, never report zeros.

    OMN-13717 (this module's original subject) fixed the *connection target* but
    left the swallow intact: ``_get_all_tables`` still returned a bare ``[]`` when
    psql itself failed, so an unreachable database aggregated to ``tables_empty=0``
    — byte-identical to a clean scan of a healthy database. That fail-open path is
    why five empty ledger tables (OMN-14525) sat undetected: the detector built to
    find them reported ``tables_empty: 0`` because it scanned nothing at all.

    Each test below fails against the pre-fix handler.
    """

    def test_psql_failure_is_a_scan_error_not_an_empty_list(self) -> None:
        """rc != 0 (unreachable host) => scan_error set, NOT a silent empty list."""

        def failing_psql(query: str, database: str) -> tuple[int, str, str]:
            return (
                255,
                "",
                "ssh: connect to host runtime-lane port 22: Operation timed out",
            )

        tables, scan_error = _get_all_tables("omnidash_analytics", failing_psql)

        assert tables == []
        assert scan_error, (
            "a failed psql probe must surface an error, not an empty list"
        )
        assert "omnidash_analytics" in scan_error
        assert "Operation timed out" in scan_error

    def test_zero_tables_is_a_scan_error(self) -> None:
        """A projection DB reporting zero tables means the probe reached nothing."""

        def empty_psql(query: str, database: str) -> tuple[int, str, str]:
            return 0, "", ""

        tables, scan_error = _get_all_tables("omnidash_analytics", empty_psql)

        assert tables == []
        assert "zero tables" in scan_error

    def test_handler_reports_error_status_when_scan_fails(self) -> None:
        """THE REGRESSION: a failed scan must NOT be reported as tables_empty=0.

        Pre-fix, this returned status='issues_found' (from unrelated migration
        errors) with tables_empty=0 — a result indistinguishable from a healthy
        database. The caller had no way to know nothing was scanned.
        """

        def failing_psql(query: str, database: str) -> tuple[int, str, str]:
            return 255, "", "connection refused"

        handler = h.NodeDatabaseSweep(psql_runner=failing_psql)
        result = handler.handle(h.DatabaseSweepRequest(omni_home="", dry_run=True))

        assert result.status == "error", (
            "a sweep that scanned zero tables because the probe failed must be "
            f"status=error, got {result.status!r} with tables_empty={result.tables_empty}"
        )
        assert result.table_scan_error
        assert result.table_results == []

    def test_successful_scan_still_reports_normally(self) -> None:
        """Regression guard: a working probe is unaffected by the fail-closed path."""

        def working_psql(query: str, database: str) -> tuple[int, str, str]:
            if "pg_tables" in query:
                return 0, "delegation_events\n", ""
            if "column_name" in query or "information_schema" in query:
                return 0, "1", ""
            return 0, "42|2026-07-13 02:00:00|HEALTHY", ""

        handler = h.NodeDatabaseSweep(psql_runner=working_psql)
        result = handler.handle(h.DatabaseSweepRequest(omni_home="", dry_run=True))

        assert result.table_scan_error == ""
        assert result.status != "error"
        assert len(result.table_results) == 1
