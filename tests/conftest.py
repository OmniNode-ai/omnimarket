# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared test fixtures for omnimarket golden chain and integration tests."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator, Callable, Generator
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest
import pytest_asyncio
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig


@pytest.fixture
def event_bus() -> EventBusInmemory:
    """Create a fresh in-memory event bus for testing."""
    return EventBusInmemory(environment="test", group="omnimarket-test")


# ---------------------------------------------------------------------------
# Integration fixtures (only active under @pytest.mark.integration)
# ---------------------------------------------------------------------------

_POSTGRES_HOST = os.environ.get("INTEGRATION_POSTGRES_HOST", "192.168.86.201")
_POSTGRES_PORT = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5436"))
_POSTGRES_USER = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
_POSTGRES_PASSWORD = os.environ.get(
    "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
)
_POSTGRES_DB = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")

_KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")


def _integration_dsn() -> str:
    return (
        f"postgresql://{quote_plus(_POSTGRES_USER)}:{quote_plus(_POSTGRES_PASSWORD)}"
        f"@{_POSTGRES_HOST}:{_POSTGRES_PORT}/{_POSTGRES_DB}"
    )


@pytest_asyncio.fixture
async def postgres_fixture(
    request: pytest.FixtureRequest,
) -> AsyncGenerator[asyncpg.Connection, None]:
    """Real asyncpg connection to 192.168.86.201:5436.

    Skips automatically when not under @pytest.mark.integration or when
    POSTGRES_PASSWORD is unset (CI without .env).
    """
    if not request.node.get_closest_marker("integration"):
        pytest.skip("postgres_fixture requires @pytest.mark.integration")
    if not _POSTGRES_PASSWORD:
        pytest.skip("POSTGRES_PASSWORD not set — skipping integration postgres fixture")
    conn: asyncpg.Connection = await asyncpg.connect(_integration_dsn())
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def integration_event_bus() -> Generator[EventBusInmemory, None, None]:
    """Fresh EventBusInmemory scoped to an integration test.

    Provides the same interface as event_bus but named distinctly so tests
    can assert bus.published after handler invocation.
    """
    bus = EventBusInmemory(
        environment="integration-test", group="omnimarket-integration"
    )
    return bus


@pytest_asyncio.fixture
async def kafka_integration_bus(
    request: pytest.FixtureRequest,
) -> AsyncGenerator[EventBusKafka, None]:
    """Real Kafka-backed event bus wired to KAFKA_BOOTSTRAP_SERVERS.

    Defaults to localhost:19092 (matches docker-compose.e2e.yml Redpanda port).
    Skips automatically when not under @pytest.mark.integration.

    Topic auto-creation is handled by the e2e compose redpanda-topic-manager
    service. For ad-hoc topics used in tests, callers should publish with
    auto.create.topics.enable (Redpanda default: on).
    """
    if not request.node.get_closest_marker("integration"):
        pytest.skip("kafka_integration_bus requires @pytest.mark.integration")

    config = ModelKafkaEventBusConfig(
        bootstrap_servers=_KAFKA_BOOTSTRAP,
        environment="integration-test",
        timeout_seconds=10,
        max_retry_attempts=1,
        retry_backoff_base=0.1,
        circuit_breaker_threshold=5,
        circuit_breaker_reset_timeout=30.0,
        consumer_sleep_interval=0.05,
        enable_idempotence=False,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        dead_letter_topic=None,
        instance_id=None,
        reconnect_backoff_ms=500,
        reconnect_backoff_max_ms=2000,
    )
    bus = EventBusKafka(config=config)
    await bus.start()
    try:
        yield bus
    finally:
        await bus.close()


_ALLOWED_TABLES: frozenset[str] = frozenset(
    {
        # omnibase_infra migration tables
        "agent_actions",
        "agent_detection_failures",
        "agent_execution_logs",
        "agent_identities",
        "agent_learnings",
        "agent_routing_decisions",
        "agent_session_snapshots",
        "agent_status_events",
        "agent_transformation_events",
        "baselines",
        "baselines_breakdown",
        "baselines_comparisons",
        "baselines_trend",
        "build_loop_cycles",
        "capability_scores",
        "change_frames",
        "ci_failure_events",
        "consumer_health_events",
        "consumer_health_triage",
        "consumer_restart_state",
        "context_audit_events",
        "context_enrichment_events",
        "contracts",
        "db_error_tickets",
        "db_metadata",
        "debug_fix_records",
        "debug_trigger_records",
        "decision_conflicts",
        "decision_store",
        "delta_bundles",
        "delta_metrics_by_model",
        "domain_taxonomy",
        "event_ledger",
        "failure_signatures",
        "failure_streaks",
        "finding_fix_pairs",
        "fix_transitions",
        "frame_pr_association",
        "fsm_state",
        "fsm_state_history",
        "gmail_intent_evaluations",
        "injection_effectiveness",
        "injection_recorded_events",
        "latency_breakdowns",
        "learned_patterns",
        "llm_call_metrics",
        "llm_cost_aggregates",
        "llm_routing_decisions",
        "manifest_injection_lifecycle",
        "merge_gate_decisions",
        "objective_evaluations",
        "pattern_candidates",
        "pattern_disable_events",
        "pattern_hit_rates",
        "pattern_injections",
        "pattern_learning_artifacts",
        "pattern_lifecycle",
        "pattern_lifecycle_transitions",
        "pattern_measured_attributions",
        "plan_reviewer_model_accuracy",
        "plan_reviewer_strategy_runs",
        "pr_envelopes",
        "registration_projections",
        "review_findings",
        "review_fixes",
        "router_performance_metrics",
        "routing_feedback_scores",
        "routing_outcomes",
        "runtime_error_triage",
        "schema_migrations",
        "session_outcomes",
        "sessions",
        "skill_executions",
        "topics",
        "user_persona_snapshots",
        "validation_event_ledger",
        "workflow_executions",
        "workflow_steps",
        # omnimarket node migration tables
        "nightly_loop_decisions",
        "nightly_loop_iterations",
        "review_bot_bypass_log",
    }
)


async def wait_for_db_row(
    conn: asyncpg.Connection,
    table: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.25,
) -> dict[str, Any]:
    """Poll a Postgres table until a row matching predicate appears.

    Args:
        conn: asyncpg connection (from postgres_fixture)
        table: Unqualified table name to query
        predicate: Callable that receives a row dict and returns True when found
        timeout: Maximum seconds to wait before raising TimeoutError
        poll_interval: Seconds between polls

    Returns:
        First matching row as a dict

    Raises:
        ValueError: If table is not in the known allowlist
        TimeoutError: If no matching row appears within timeout seconds
    """
    if table not in _ALLOWED_TABLES:
        raise ValueError(
            f"Unknown table: {table!r} — add it to _ALLOWED_TABLES in conftest.py"
        )
    deadline = time.monotonic() + timeout
    while True:
        rows = await conn.fetch(f"SELECT * FROM {table}")
        for row in rows:
            row_dict = dict(row)
            if predicate(row_dict):
                return row_dict
        if time.monotonic() >= deadline:
            raise TimeoutError(f"No matching row found in {table!r} within {timeout}s")
        await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Lint guard: reject EventBusInmemory imports in tests/integration/*
# ---------------------------------------------------------------------------


def pytest_collect_file(parent: pytest.Collector, file_path: Any) -> None:
    """Block any integration test that imports EventBusInmemory."""
    import ast

    path_str = str(file_path)
    if "/tests/integration/" not in path_str or not path_str.endswith(".py"):
        return

    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=path_str)
    except (OSError, SyntaxError):
        return

    for node in ast.walk(tree):
        is_forbidden_from_import = (
            isinstance(node, ast.ImportFrom)
            and node.names
            and (
                any(alias.name == "EventBusInmemory" for alias in node.names)
                or (node.module and "event_bus_inmemory" in node.module)
            )
        )
        is_forbidden_module_import = isinstance(node, ast.Import) and any(
            "event_bus_inmemory" in alias.name for alias in node.names
        )
        if is_forbidden_from_import or is_forbidden_module_import:
            pytest.fail(
                f"[OMN-8726] {path_str} imports EventBusInmemory — "
                "integration tests must use kafka_integration_bus fixture, "
                "not the in-memory bus."
            )
