# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-10580 reason="test fixture — lab endpoints used as fixture defaults for golden chain tests; not runtime defaults"
"""Shared test fixtures for omnimarket golden chain and integration tests."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import AsyncGenerator, Callable, Generator
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import omnibase_core
import omnibase_infra
import pytest
import pytest_asyncio

from omnimarket.config.env_flags import env_flag

# =============================================================================
# Hermetic import guard (OMN-14420 / OMN-15019) — omnimarket-side
# =============================================================================
# omnibase_core / omnibase_infra are THIRD-PARTY pinned dependencies here
# (resolved from this repo's own .venv/site-packages per uv.lock), NOT
# editable worktree sources. Unlike a repo importing its OWN package (where
# this repo's `[tool.pytest.ini_options] pythonpath = ["src", "."]` setting
# inserts this repo's own src/ ahead of everything else on sys.path), that
# ini setting does nothing to protect a THIRD-PARTY dependency's resolution.
#
# An ambient PYTHONPATH exported by the parent shell/session (OMN-14420:
# "Ambient PYTHONPATH makes worktree test runs silently execute
# canonical-clone source") is inserted into sys.path by the interpreter
# itself, ahead of site-packages. If it happens to contain a sibling
# canonical clone's `omnibase_core/src` or `omnibase_infra/src`,
# `import omnibase_core` / `import omnibase_infra` silently resolves to a
# different — possibly stale, possibly incompatible — copy than the one
# uv.lock actually pinned and installed, instead of failing or warning.
#
# OMN-15019 reproduced exactly this shape: a `ModuleNotFoundError` diagnosed
# as an omnibase_infra packaging gap disappeared under `env -u PYTHONPATH`
# and reproduced identically after a from-scratch `.venv` rebuild — the
# failure was never about packaging, it was ambient PYTHONPATH redirecting
# the import to a stale sibling clone. omnibase_infra#2423 shipped the
# same-shaped guard for that repo's OWN package; this is the omnimarket-side
# mirror for its two cross-repo runtime dependencies.
#
# NOT every out-of-venv resolution is an accidental shadow: a small number of
# CI jobs (e.g. .github/workflows/validator-fsm-handler-drift.yml)
# DELIBERATELY point PYTHONPATH at a freshly-checked-out sibling clone to
# exercise a specific cross-repo validator against it. There is no reliable
# metadata signal that distinguishes that intentional case from an
# accidental ambient shadow (uv's `pip install -e --no-deps` for a foreign
# package does not reliably record a PEP 660 editable direct_url.json here),
# so the escape hatch is an explicit, visible, opt-in env var that the
# intentional-override job sets alongside its own PYTHONPATH -- never a
# silent narrowing of this guard's default. Mirrors the codebase's existing
# declared-escape-hatch pattern (e.g. OMNIMARKET_LEGACY_MERGE_ARM_ENABLED
# above): the override must be visible in the workflow YAML, not inferred.
#
# Fail LOUD at collection time — never a silent fallback or narrowing — with
# the exact remediation inline, instead of a confusing downstream
# ModuleNotFoundError/AttributeError many tests later that looks like an
# unrelated packaging or code bug. Runs BEFORE any `from omnibase_X.sub import
# ...` submodule import below, so a shadowed-but-incomplete sibling clone is
# correctly diagnosed here instead of surfacing as an unrelated-looking
# ModuleNotFoundError on a submodule.
_HERMETIC_GUARD_OVERRIDE_ENV = "OMNIMARKET_ALLOW_PYTHONPATH_OVERRIDE"
if env_flag(_HERMETIC_GUARD_OVERRIDE_ENV, safe_default=False):
    print(
        f"[hermetic-import-guard] {_HERMETIC_GUARD_OVERRIDE_ENV} is set -- "
        "skipping the OMN-14420 ambient-PYTHONPATH guard for this run "
        "(declared intentional cross-repo dependency override).",
        file=sys.stderr,
    )
else:
    _VENV_ROOT = Path(sys.prefix).resolve()
    for _dep_module in (omnibase_core, omnibase_infra):
        _dep_file = _dep_module.__file__
        if _dep_file is None:
            raise RuntimeError(
                f"Hermetic import guard failed: `{_dep_module.__name__}` has "
                "no __file__ (unexpected namespace-package resolution) -- "
                "cannot verify it resolved from this repo's venv. See "
                "OMN-14420."
            )
        _resolved_dep_path = Path(_dep_file).resolve()
        if _VENV_ROOT not in _resolved_dep_path.parents:
            raise RuntimeError(
                f"Hermetic import guard failed: `{_dep_module.__name__}` "
                f"resolved OUTSIDE this repo's venv ({_VENV_ROOT}); got "
                f"{_resolved_dep_path} instead. This almost always means an "
                "ambient PYTHONPATH in your shell is shadowing the pinned, "
                "installed dependency with a different (possibly stale) "
                "sibling clone — see OMN-14420. Re-run with "
                "'env -u PYTHONPATH' prefixed on your command. If this "
                f"PYTHONPATH override is intentional, set "
                f"{_HERMETIC_GUARD_OVERRIDE_ENV}=1 to declare it explicitly."
            )

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory  # noqa: E402
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka  # noqa: E402
from omnibase_infra.event_bus.models.config import (  # noqa: E402
    ModelKafkaEventBusConfig,
)


@pytest.fixture
def event_bus() -> EventBusInmemory:
    """Create a fresh in-memory event bus for testing."""
    return EventBusInmemory(environment="test", group="omnimarket-test")


# ---------------------------------------------------------------------------
# Canonical portable fixtures — use these instead of hardcoded literals
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_postgres_dsn() -> str:
    """Safe unit-test DSN — never reaches a real database."""
    return "postgresql://test:test@localhost:5432/test_db"


@pytest.fixture
def fake_kafka_bootstrap() -> str:
    """Safe unit-test Kafka bootstrap address."""
    return "localhost:9092"


@pytest.fixture
def fake_omni_home(tmp_path: Path) -> Path:
    """Isolated tmp directory standing in for OMNI_HOME / user home paths."""
    home = tmp_path / "omni_home"
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture(autouse=True)
def _ensure_omni_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Set OMNI_HOME to a tmp dir for any test that runs without it in the env.

    Guards against os.environ["OMNI_HOME"] KeyErrors in handlers that read
    OMNI_HOME directly (fail-fast pattern introduced in OMN-10646).
    """
    if not os.environ.get("OMNI_HOME"):
        monkeypatch.setenv("OMNI_HOME", str(tmp_path / "omni_home"))


@pytest.fixture(autouse=True)
def _ensure_delegation_routing_tiers_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind DELEGATION_ROUTING_TIERS_PATH to the canonical packaged file by
    default (OMN-15628).

    ``node_delegation_routing_reducer``'s config loader now fails fast (no
    packaged-default fallback, CLAUDE.md rule 8) when this key is unbound.
    Mirrors the ``_ensure_omni_home`` pattern above (OMN-10646): most tests
    exercise the ROUTING behavior, not the binding itself, so this fixture
    supplies the explicit binding the suite otherwise relied on implicitly.
    Tests that specifically prove the RED refusal path unset it via
    ``monkeypatch.delenv`` inside the test body, which runs after fixture
    setup and therefore wins.
    """
    if not os.environ.get("DELEGATION_ROUTING_TIERS_PATH"):
        from omnimarket.routing.routing_tiers_path import (
            ROUTING_TIERS_PACKAGED_DEFAULT_PATH,
        )

        monkeypatch.setenv(
            "DELEGATION_ROUTING_TIERS_PATH", str(ROUTING_TIERS_PACKAGED_DEFAULT_PATH)
        )


@pytest.fixture(autouse=True)
def _ensure_bifrost_contract_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind BIFROST_CONTRACT_PATH to the canonical packaged file by default
    (OMN-15628).

    ``_load_bifrost_endpoints()`` now refuses to boot (no packaged-default
    fallback, CLAUDE.md rule 8) when NEITHER BIFROST_CONTRACT_PATH nor
    BIFROST_OVERLAY_PATH is bound. Mirrors ``_ensure_delegation_routing_tiers_path``
    above. Tests that specifically prove the RED refusal path (or that set
    BIFROST_OVERLAY_PATH/BIFROST_CONTRACT_PATH themselves) unset/override this
    via ``monkeypatch`` inside the test body or a more specific fixture, which
    runs after this autouse fixture's setup and therefore wins.
    """
    if not os.environ.get("BIFROST_CONTRACT_PATH") and not os.environ.get(
        "BIFROST_OVERLAY_PATH"
    ):
        from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
            _DEFAULT_CONFIG_PATH as _BIFROST_DEFAULT_CONFIG_PATH,
        )

        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(_BIFROST_DEFAULT_CONFIG_PATH))


@pytest.fixture(autouse=True)
def _scrub_inherited_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """OMN-14746: unset git plumbing env vars inherited from the pre-push hook.

    When ``git push`` invokes a hook, git exports ``GIT_DIR`` / ``GIT_INDEX_FILE``
    / ``GIT_WORK_TREE`` (absolute paths into the REAL invoking worktree's ``.git``).
    Those override BOTH ``subprocess.run(..., cwd=<tmp>)`` AND an explicit
    ``git -C <tmp>`` (memory ``reference_git_env_vars_override_c_and_cwd``), so any
    test that runs ``git init/add/commit`` in a ``tmp_path`` repo would instead
    mutate — and wipe — the invoking worktree's index (the observed ~5846 staged
    deletions). Popping them from ``os.environ`` makes every git subprocess (the
    test helper AND the production handler under test) operate on its own ``cwd``.

    Under a plain ``uv run pytest`` (no hook) these vars are unset, so this is a
    no-op there — safe and correct. This is a function-scoped autouse fixture, so
    it runs before the function-scoped ``git_repo`` / ``clone`` fixtures that build
    tmp repos, scrubbing the env before any git subprocess fires.
    """
    for var in (
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_WORK_TREE",
        "GIT_PREFIX",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _isolate_unit_env(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Isolate unit tests from live-infra env vars.

    Guards two classes of pre-existing failures:

    1. Kafka hang (OMN-13068 Cluster A/C): unit tests that construct
       BaseProjectionRunner subclasses call _emit_terminal_event after a
       successful DB write.  When KAFKA_BOOTSTRAP_SERVERS points at a live
       broker, AIOKafkaProducer.start() blocks indefinitely.  Unit tests must
       not reach real brokers — clear the broker env vars so the suppress path
       in BaseProjectionRunner.__init__ takes the no-binding branch.

    2. Replay-state pollution (OMN-13068 Cluster B/E): HandlerGeneratedExecutor
       persists replay markers under ONEX_STATE_DIR.  Tests that use hardcoded
       correlation IDs (e.g. "corr-3", "omn-12831-golden-chain-001") hit a
       stale replay file written by a previous run and skip the emit, causing
       assertion failures.  Redirect ONEX_STATE_DIR (and its alias
       ONEX_STATE_ROOT) to the per-test tmp_path so no cross-run state leaks.

    Only tests that live under ``tests/integration/`` (the true integration
    suite that requires live Kafka/DB) are exempted.  Tests outside that
    directory that carry ``@pytest.mark.integration`` are golden-chain or
    in-memory tests that must still be isolated from live-infra env vars.
    """
    test_path = str(request.node.fspath)
    if "/tests/integration/" in test_path:
        return

    # Clear Kafka broker env vars so unit tests never reach a live broker.
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    monkeypatch.delenv("KAFKA_BROKER", raising=False)
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)

    # Redirect node-generation-consumer replay state to an isolated tmp dir.
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "onex_state"))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _default_paid_escalation_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """OMN-14225: run the suite at the production DEFAULT — paid escalation ON.

    Paid (metered) escalation is ON by default (metered + logged, never silent); an
    operator opts OUT via a falsy ``ONEX_DELEGATION_ALLOW_PAID``. Clear any ambient
    opt-out so the many escalation-mechanism tests reliably exercise the FULL ladder
    (they assert escalation into the paid ``cheap_cloud``/``claude`` tiers). The
    opt-OUT behavior is covered by dedicated regressions
    (``test_paid_escalation_gate_omn14225``) that ``setenv`` a falsy value.
    """
    monkeypatch.delenv("ONEX_DELEGATION_ALLOW_PAID", raising=False)


_LEGACY_ARM_BEHAVIOR_TESTS = frozenset(
    {
        "tests/integration/test_merge_sweep_triage_orchestrator_route_coverage.py",
        "tests/nodes/node_auto_merge_effect/test_handler_auto_merge_effect.py",
        "tests/test_auto_merge_arm_effect.py",
        "tests/test_golden_chain_auto_merge_effect.py",
        "tests/test_golden_chain_merge_sweep_executor.py",
        "tests/test_triage_orchestrator.py",
        "tests/test_triage_phase2_emit_rules.py",
    }
)


@pytest.fixture(autouse=True)
def _enable_legacy_arm_for_direct_behavior_tests(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt legacy direct-behavior suites into the OMN-14151 legacy arm surface.

    The shipped default remains fail-closed. These suites exercise the old
    handlers/routes directly, so they must opt in explicitly instead of
    weakening the production default.
    """
    try:
        relative_path = Path(request.node.fspath).relative_to(Path.cwd()).as_posix()
    except ValueError:
        relative_path = Path(request.node.fspath).as_posix()
    if relative_path in _LEGACY_ARM_BEHAVIOR_TESTS:
        monkeypatch.setenv("OMNIMARKET_LEGACY_MERGE_ARM_ENABLED", "true")


@pytest.fixture
def fake_lan_ip() -> str:
    """Loopback address used in unit tests instead of a LAN IP."""
    return "127.0.0.1"


@pytest.fixture
def fake_llm_url() -> str:
    """Localhost LLM base URL for unit tests."""
    return "http://localhost:8000"


@pytest.fixture
def mock_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Settings instance wired with test values, env monkeypatched to match."""
    from omnimarket.config.settings import Settings

    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DATABASE", "test_db")
    monkeypatch.setenv("POSTGRES_USER", "test")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test")
    monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")
    monkeypatch.setenv("LLM_CODER_FAST_URL", "http://localhost:8001")
    monkeypatch.setenv("LLM_REASONER_URL", "http://localhost:8001")
    monkeypatch.setenv("LLM_EMBEDDING_URL", "http://localhost:8100")

    return Settings(_env_file=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Integration fixtures (only active under @pytest.mark.integration)
# ---------------------------------------------------------------------------

_POSTGRES_HOST = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
_POSTGRES_PORT = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
_POSTGRES_USER = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
_POSTGRES_PASSWORD = os.environ.get(
    "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
)
_POSTGRES_DB = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")

_KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")


def _integration_dsn() -> str:
    return (
        f"postgresql://{quote_plus(_POSTGRES_USER)}:{quote_plus(_POSTGRES_PASSWORD)}"
        f"@{_POSTGRES_HOST}:{_POSTGRES_PORT}/{_POSTGRES_DB}"
    )


@pytest_asyncio.fixture
async def postgres_fixture(
    request: pytest.FixtureRequest,
) -> AsyncGenerator[asyncpg.Connection, None]:
    """Real asyncpg connection — reads INTEGRATION_POSTGRES_HOST from env.

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

    Defaults to localhost:9092. Skips automatically when not under
    @pytest.mark.integration.

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
        "delegation_events",
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
        # node_projection_live_events
        "live_events",
        # node_projection_skill_executions
        "skill_execution_snapshots",
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
