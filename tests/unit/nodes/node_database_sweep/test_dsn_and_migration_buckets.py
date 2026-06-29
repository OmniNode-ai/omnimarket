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

        tables = _get_all_tables("omnidash_analytics", fake_psql)

        assert tables == ["agent_routing_decisions", "node_service_registry"]


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
