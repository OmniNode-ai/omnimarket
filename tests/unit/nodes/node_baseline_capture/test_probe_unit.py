"""Unit tests for baseline capture probe modules.

Covers the four infra-dependent probes that have no dedicated test coverage:
- probe_system_health   (httpx, env-configured service URLs)
- probe_kafka_topics    (httpx, Redpanda admin API)
- probe_db_row_counts   (asyncpg, Postgres)
- probe_linear_tickets  (httpx, Linear GraphQL API)

No subprocess, no network. All I/O is patched or env-gated.

OMN-11588 — wire 6 probe scaffolds into baseline_capture.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.nodes.node_baseline_capture.handlers.probes import (
    probe_db_row_counts,
    probe_git_branches,
    probe_github_prs,
    probe_kafka_topics,
    probe_linear_tickets,
    probe_system_health,
)
from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    BaselineProbeType,
    ModelDbRowCountSnapshot,
    ModelKafkaTopicSnapshot,
    ModelLinearTicketSnapshot,
    ModelServiceHealthSnapshot,
)

# ---------------------------------------------------------------------------
# Probe registration sanity — all 6 are importable and have the right name attr
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_six_probe_modules_importable() -> None:
    """All 6 probe modules must be importable and expose a probe class with .name."""
    assert probe_github_prs.ProbeGitHubPRs.name == BaselineProbeType.GITHUB_PRS
    assert (
        probe_linear_tickets.ProbeLinearTickets.name == BaselineProbeType.LINEAR_TICKETS
    )
    assert probe_system_health.ProbeSystemHealth.name == BaselineProbeType.SYSTEM_HEALTH
    assert probe_kafka_topics.ProbeKafkaTopics.name == BaselineProbeType.KAFKA_TOPICS
    assert probe_git_branches.ProbeGitBranches.name == BaselineProbeType.GIT_BRANCHES
    assert probe_db_row_counts.ProbeDbRowCounts.name == BaselineProbeType.DB_ROW_COUNTS


@pytest.mark.unit
def test_handler_default_registry_contains_all_six_probes() -> None:
    """The lazy-built handler registry must contain all BaselineProbeType members."""
    from omnimarket.nodes.node_baseline_capture.handlers.handler_baseline_capture import (
        HandlerBaselineCapture,
    )

    handler = HandlerBaselineCapture()
    registry = handler._get_registry()
    assert set(registry.keys()) == set(BaselineProbeType)


# ---------------------------------------------------------------------------
# probe_system_health
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProbeSystemHealth:
    """Unit tests for ProbeSystemHealth — patches httpx.AsyncClient."""

    async def test_returns_empty_when_no_services_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no env vars set, _services_from_env returns nothing and collect is empty."""
        for var in (
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "REDPANDA_ADMIN_HOST",
            "VALKEY_HOST",
            "VALKEY_PORT",
            "QDRANT_HOST",
            "QDRANT_PORT",
            "LLM_CODER_URL",
            "LLM_CODER_FAST_URL",
            "LLM_EMBEDDING_URL",
        ):
            monkeypatch.delenv(var, raising=False)

        result = await probe_system_health.ProbeSystemHealth().collect("/tmp/omni_home")
        assert result == []

    async def test_healthy_service_returns_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reachable service with status 200 produces healthy=True snapshot."""
        monkeypatch.setenv("REDPANDA_ADMIN_HOST", "redpanda.test.invalid")
        monkeypatch.setenv("REDPANDA_ADMIN_PORT", "9644")
        for var in (
            "POSTGRES_HOST",
            "VALKEY_HOST",
            "QDRANT_HOST",
            "LLM_CODER_URL",
            "LLM_CODER_FAST_URL",
            "LLM_EMBEDDING_URL",
        ):
            monkeypatch.delenv(var, raising=False)

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await probe_system_health.ProbeSystemHealth().collect(
                "/tmp/omni_home"
            )

        assert len(result) == 1
        item = result[0]
        assert isinstance(item, ModelServiceHealthSnapshot)
        assert item.service == "redpanda"
        assert item.healthy is True
        assert item.error is None

    async def test_unreachable_service_records_healthy_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A service that raises ConnectError is recorded with healthy=False."""
        monkeypatch.setenv("REDPANDA_ADMIN_HOST", "redpanda.test.invalid")
        monkeypatch.setenv("REDPANDA_ADMIN_PORT", "9644")
        for var in (
            "POSTGRES_HOST",
            "VALKEY_HOST",
            "QDRANT_HOST",
            "LLM_CODER_URL",
            "LLM_CODER_FAST_URL",
            "LLM_EMBEDDING_URL",
        ):
            monkeypatch.delenv(var, raising=False)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=OSError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await probe_system_health.ProbeSystemHealth().collect(
                "/tmp/omni_home"
            )

        assert len(result) == 1
        item = result[0]
        assert isinstance(item, ModelServiceHealthSnapshot)
        assert item.healthy is False
        assert item.error is not None

    async def test_http_5xx_marks_unhealthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 500 from a service records healthy=False."""
        monkeypatch.setenv("LLM_CODER_URL", "http://llm.test.invalid:8000")
        for var in (
            "POSTGRES_HOST",
            "REDPANDA_ADMIN_HOST",
            "VALKEY_HOST",
            "QDRANT_HOST",
            "LLM_CODER_FAST_URL",
            "LLM_EMBEDDING_URL",
        ):
            monkeypatch.delenv(var, raising=False)

        mock_response = MagicMock()
        mock_response.status_code = 503

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await probe_system_health.ProbeSystemHealth().collect(
                "/tmp/omni_home"
            )

        assert len(result) == 1
        assert result[0].healthy is False
        assert "503" in (result[0].error or "")

    async def test_returns_empty_when_httpx_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When httpx is not installed, probe returns [] gracefully."""
        monkeypatch.setenv("LLM_CODER_URL", "http://llm.test.invalid:8000")
        for var in (
            "POSTGRES_HOST",
            "REDPANDA_ADMIN_HOST",
            "VALKEY_HOST",
            "QDRANT_HOST",
            "LLM_CODER_FAST_URL",
            "LLM_EMBEDDING_URL",
        ):
            monkeypatch.delenv(var, raising=False)

        with patch.dict("sys.modules", {"httpx": None}):
            result = await probe_system_health.ProbeSystemHealth().collect(
                "/tmp/omni_home"
            )

        assert result == []


# ---------------------------------------------------------------------------
# probe_kafka_topics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProbeKafkaTopics:
    """Unit tests for ProbeKafkaTopics — patches httpx.AsyncClient."""

    async def test_returns_empty_when_host_not_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When REDPANDA_ADMIN_HOST is unset, probe returns [] gracefully."""
        monkeypatch.delenv("REDPANDA_ADMIN_HOST", raising=False)
        result = await probe_kafka_topics.ProbeKafkaTopics().collect("/tmp/omni_home")
        assert result == []

    async def test_collects_topics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With a valid admin response, topics are collected as ModelKafkaTopicSnapshot."""
        monkeypatch.setenv("REDPANDA_ADMIN_HOST", "redpanda.test.invalid")
        monkeypatch.setenv("REDPANDA_ADMIN_PORT", "9644")

        topics_json: list[dict[str, Any]] = [
            {
                "name": "onex.evt.omnimarket.baseline-captured.v1",
                "partitions": [{"latest_offset": 50}, {"latest_offset": 30}],
            },
            {
                "name": "onex.cmd.omnimarket.baseline-capture-start.v1",
                "partitions": [{"latest_offset": 10}],
            },
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=topics_json)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await probe_kafka_topics.ProbeKafkaTopics().collect(
                "/tmp/omni_home"
            )

        assert len(result) == 2
        assert all(isinstance(item, ModelKafkaTopicSnapshot) for item in result)
        names = {item.topic for item in result}  # type: ignore[union-attr]
        assert "onex.evt.omnimarket.baseline-captured.v1" in names
        assert "onex.cmd.omnimarket.baseline-capture-start.v1" in names
        # Offset totals
        captured = next(
            i
            for i in result
            if i.topic == "onex.evt.omnimarket.baseline-captured.v1"  # type: ignore[union-attr]
        )
        assert captured.latest_offset == 80  # type: ignore[union-attr]

    async def test_internal_topics_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Topics starting with '_' (internal) are excluded from results."""
        monkeypatch.setenv("REDPANDA_ADMIN_HOST", "redpanda.test.invalid")

        topics_json = [
            {"name": "_internal_topic", "partitions": []},
            {"name": "onex.evt.test.v1", "partitions": [{"latest_offset": 5}]},
        ]

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=topics_json)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await probe_kafka_topics.ProbeKafkaTopics().collect(
                "/tmp/omni_home"
            )

        assert len(result) == 1
        assert result[0].topic == "onex.evt.test.v1"  # type: ignore[union-attr]

    async def test_returns_empty_on_admin_api_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When admin API raises, probe returns [] non-fatally."""
        monkeypatch.setenv("REDPANDA_ADMIN_HOST", "redpanda.test.invalid")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=OSError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await probe_kafka_topics.ProbeKafkaTopics().collect(
                "/tmp/omni_home"
            )

        assert result == []

    async def test_returns_empty_when_httpx_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When httpx is not installed, probe returns [] gracefully."""
        monkeypatch.setenv("REDPANDA_ADMIN_HOST", "redpanda.test.invalid")
        with patch.dict("sys.modules", {"httpx": None}):
            result = await probe_kafka_topics.ProbeKafkaTopics().collect(
                "/tmp/omni_home"
            )
        assert result == []


# ---------------------------------------------------------------------------
# probe_db_row_counts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProbeDbRowCounts:
    """Unit tests for ProbeDbRowCounts — patches asyncpg."""

    async def test_returns_empty_when_db_url_not_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When OMNIBASE_INFRA_DB_URL is unset, probe returns [] gracefully."""
        monkeypatch.delenv("OMNIBASE_INFRA_DB_URL", raising=False)
        result = await probe_db_row_counts.ProbeDbRowCounts().collect("/tmp/omni_home")
        assert result == []

    async def test_returns_empty_when_asyncpg_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When asyncpg is not installed, probe returns [] gracefully."""
        monkeypatch.setenv("OMNIBASE_INFRA_DB_URL", "postgresql://localhost/test")
        with patch.dict("sys.modules", {"asyncpg": None}):
            result = await probe_db_row_counts.ProbeDbRowCounts().collect(
                "/tmp/omni_home"
            )
        assert result == []

    async def test_collects_row_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With a valid DB connection, row counts are returned for each table."""
        monkeypatch.setenv(
            "OMNIBASE_INFRA_DB_URL", "postgresql://localhost:5436/omnibase"
        )

        mock_conn = AsyncMock()

        def make_row(count: int) -> MagicMock:
            row = MagicMock()
            row.__getitem__ = MagicMock(
                side_effect=lambda k: count if k == "cnt" else 0
            )
            return row

        mock_conn.fetchrow = AsyncMock(side_effect=[make_row(c) for c in range(7)])
        mock_conn.close = AsyncMock()

        mock_asyncpg = MagicMock()
        mock_asyncpg.connect = AsyncMock(return_value=mock_conn)

        with patch.dict("sys.modules", {"asyncpg": mock_asyncpg}):
            result = await probe_db_row_counts.ProbeDbRowCounts().collect(
                "/tmp/omni_home"
            )

        assert len(result) == len(probe_db_row_counts._KEY_TABLES)
        assert all(isinstance(item, ModelDbRowCountSnapshot) for item in result)
        # Row count for the first table should be 0 (side_effect index 0 → count=0)
        assert result[0].row_count == 0  # type: ignore[union-attr]
        # Connection must be closed even on success
        mock_conn.close.assert_called_once()

    async def test_returns_empty_on_connection_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When asyncpg.connect raises, probe returns [] non-fatally."""
        monkeypatch.setenv(
            "OMNIBASE_INFRA_DB_URL", "postgresql://localhost:5436/omnibase"
        )

        mock_asyncpg = MagicMock()
        mock_asyncpg.connect = AsyncMock(side_effect=OSError("Connection refused"))

        with patch.dict("sys.modules", {"asyncpg": mock_asyncpg}):
            result = await probe_db_row_counts.ProbeDbRowCounts().collect(
                "/tmp/omni_home"
            )

        assert result == []

    async def test_individual_table_failure_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fetchrow failure on one table is skipped; other tables still collected."""
        monkeypatch.setenv(
            "OMNIBASE_INFRA_DB_URL", "postgresql://localhost:5436/omnibase"
        )

        mock_conn = AsyncMock()

        call_count = 0

        async def fetchrow_side_effect(query: str) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Table missing")
            row = MagicMock()
            row.__getitem__ = MagicMock(side_effect=lambda k: 10 if k == "cnt" else 0)
            return row

        mock_conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
        mock_conn.close = AsyncMock()

        mock_asyncpg = MagicMock()
        mock_asyncpg.connect = AsyncMock(return_value=mock_conn)

        with patch.dict("sys.modules", {"asyncpg": mock_asyncpg}):
            result = await probe_db_row_counts.ProbeDbRowCounts().collect(
                "/tmp/omni_home"
            )

        # First table failed, remaining collected
        assert len(result) == len(probe_db_row_counts._KEY_TABLES) - 1
        assert all(isinstance(item, ModelDbRowCountSnapshot) for item in result)
        # Connection must still be closed
        mock_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# probe_linear_tickets
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProbeLinearTickets:
    """Unit tests for ProbeLinearTickets — patches httpx.AsyncClient."""

    async def test_returns_empty_when_api_key_not_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When LINEAR_API_KEY is unset, probe returns [] gracefully."""
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        result = await probe_linear_tickets.ProbeLinearTickets().collect(
            "/tmp/omni_home"
        )
        assert result == []

    async def test_returns_empty_when_httpx_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When httpx is not installed, probe returns [] gracefully."""
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test123")
        with patch.dict("sys.modules", {"httpx": None}):
            result = await probe_linear_tickets.ProbeLinearTickets().collect(
                "/tmp/omni_home"
            )
        assert result == []

    async def test_collects_linear_tickets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a valid API response, tickets are collected as ModelLinearTicketSnapshot."""
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test123")

        issues = [
            {
                "identifier": "OMN-11588",
                "title": "Wire 6 probe scaffolds",
                "state": {"name": "In Progress"},
                "priority": 3,
                "assignee": {"displayName": "jonah"},
                "updatedAt": "2026-05-22T14:29:54.932Z",
            },
            {
                "identifier": "OMN-11534",
                "title": "Parent epic",
                "state": {"name": "In Progress"},
                "priority": 2,
                "assignee": None,
                "updatedAt": "2026-05-20T10:00:00.000Z",
            },
        ]
        api_response = {"data": {"issues": {"nodes": issues}}}

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=api_response)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await probe_linear_tickets.ProbeLinearTickets().collect(
                "/tmp/omni_home"
            )

        assert len(result) == 2
        assert all(isinstance(item, ModelLinearTicketSnapshot) for item in result)
        ids = {item.ticket_id for item in result}  # type: ignore[union-attr]
        assert "OMN-11588" in ids
        assert "OMN-11534" in ids
        # Assignee handling
        omn_11588 = next(
            i
            for i in result
            if i.ticket_id == "OMN-11588"  # type: ignore[union-attr]
        )
        omn_11534 = next(
            i
            for i in result
            if i.ticket_id == "OMN-11534"  # type: ignore[union-attr]
        )
        assert omn_11588.assignee == "jonah"  # type: ignore[union-attr]
        assert omn_11534.assignee is None  # type: ignore[union-attr]

    async def test_returns_empty_on_api_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the Linear API raises, probe returns [] non-fatally."""
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test123")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=OSError("Network error"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await probe_linear_tickets.ProbeLinearTickets().collect(
                "/tmp/omni_home"
            )

        assert result == []

    async def test_malformed_issue_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An issue with an unparseable updatedAt is skipped; others still collected."""
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test123")

        issues = [
            {
                "identifier": "OMN-BAD",
                "title": "Bad ticket",
                "state": {"name": "Backlog"},
                "priority": 1,
                "assignee": None,
                "updatedAt": "not-a-date",  # malformed
            },
            {
                "identifier": "OMN-GOOD",
                "title": "Good ticket",
                "state": {"name": "In Progress"},
                "priority": 2,
                "assignee": None,
                "updatedAt": "2026-05-22T14:29:54.932Z",
            },
        ]
        api_response = {"data": {"issues": {"nodes": issues}}}

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=api_response)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await probe_linear_tickets.ProbeLinearTickets().collect(
                "/tmp/omni_home"
            )

        # Only the good ticket is collected; bad one is skipped
        assert len(result) == 1
        assert result[0].ticket_id == "OMN-GOOD"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# ProbeProtocol conformance — all 6 probes satisfy the protocol
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_probes_satisfy_probe_protocol() -> None:
    """All 6 probe instances must satisfy the ProbeProtocol runtime check."""
    from omnimarket.nodes.node_baseline_capture.handlers.handler_baseline_capture import (
        ProbeProtocol,
    )

    probes = [
        probe_github_prs.ProbeGitHubPRs(),
        probe_linear_tickets.ProbeLinearTickets(),
        probe_system_health.ProbeSystemHealth(),
        probe_kafka_topics.ProbeKafkaTopics(),
        probe_git_branches.ProbeGitBranches(),
        probe_db_row_counts.ProbeDbRowCounts(),
    ]
    for p in probes:
        assert isinstance(p, ProbeProtocol), (
            f"{p.__class__.__name__} does not satisfy ProbeProtocol"
        )
