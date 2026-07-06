# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14001 — the first closed platform learning loop.

Proves that a stored outcome (`context_roi_scores`) actually CHANGES a live
routing decision:

  * ``build_roi_overlay`` / ``resolve_roi_overlay`` (the I/O boundary) — read
    captured rows and aggregate a deterministic per-tier ROI signal, fail-OPEN on
    any read error.
  * ``first_eligible_tier`` / ``next_eligible_tier`` / ``delta`` (the pure reducer)
    — demote an ROI-suppressed tier so the SAME request routes to a DIFFERENT tier
    than the static ``routing_tiers.yaml`` order would pick, with a fail-safe that
    keeps a fully-suppressed ladder resolvable, and byte-identical behaviour when no
    overlay is supplied (golden-chain replays unaffected).
  * ``LocalDelegationDispatchPort`` — the live ``onex delegate`` enforcement wire:
    reads the overlay (fail-open None without a projection DB) and threads it into
    the routing authority for both the initial resolution and every escalation hop.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    delta,
    first_eligible_tier,
    next_eligible_tier,
    tier_for_backend,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.routing.roi_overlay import (
    CONTEXT_ROI_TABLE,
    ModelRoutingRoiOverlay,
    ModelTierRoiSignal,
    build_roi_overlay,
    resolve_roi_min_samples,
    resolve_roi_overlay,
    resolve_roi_success_floor,
)

# --- Fixtures -------------------------------------------------------------------

# Three routable tiers for code_generation (tier_order = local -> cheap_cloud ->
# cheap_frontier -> claude): local-coder, cloud-glm (cheap_cloud AND claude
# ceiling), openrouter-qwen3-coder-480b (cheap_frontier). Complete endpoint_urls
# so the tiers route in CI without a host overlay.
_BIFROST_THREE_TIER = textwrap.dedent(
    """\
    config_version: "2.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        endpoint_url: "http://local.test:8000/v1/chat/completions"
        model_name: qwen-coder
        tier: local
        timeout_ms: 30000
        max_tokens: 8192
        capabilities: [code_generation]
      - backend_id: cloud-glm
        endpoint_url: "https://cloud.test/glm/v1/chat/completions"
        model_name: glm-5.2
        tier: cheap_cloud
        timeout_ms: 30000
        max_tokens: 8192
        capabilities: [code_generation]
      - backend_id: openrouter-qwen3-coder-480b
        endpoint_url: "https://openrouter.test/v1/chat/completions"
        model_name: qwen3-coder-480b
        tier: cheap_frontier
        timeout_ms: 30000
        max_tokens: 8192
        capabilities: [code_generation]
    routing_rules:
      - rule_id: "c0ffee00-0011-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder, cloud-glm, openrouter-qwen3-coder-480b]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "c0ffee00-0012-4000-8000-000000000001"
    default_backends:
      - local-coder
    circuit_breaker:
      failure_threshold: 5
      window_seconds: 30
    failover:
      max_attempts: 3
      backoff_base_ms: 500
    shadow_mode:
      enabled: false
      policy_version: "test"
      log_sample_rate: 1.0
      comparison_logging_enabled: true
      max_shadow_latency_ms: 5.0
    """
)


@pytest.fixture
def code_gen_routable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Point routing at a self-contained 3-tier contract so tiers route in CI."""
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_THREE_TIER)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()


def _suppress(
    *tier_names: str, task_type: str = "code_generation"
) -> ModelRoutingRoiOverlay:
    """Build an overlay that ROI-suppresses exactly ``tier_names``."""
    return ModelRoutingRoiOverlay(
        task_type=task_type,
        min_samples=5,
        success_floor=0.5,
        signals=tuple(
            ModelTierRoiSignal(
                tier_name=name,
                sample_count=6,
                success_count=0,
                success_rate=0.0,
                suppressed=True,
            )
            for name in tier_names
        ),
    )


def _roi_rows(endpoint_ref: str, *, n: int, successes: int) -> list[dict[str, object]]:
    """N context_roi_scores rows for one endpoint with ``successes`` final_success.

    Each row carries a unique ``correlation_id`` (the table's real unique index)
    so the in-memory adapter's upsert-on-conflict-key keeps every row distinct.
    """
    return [
        {
            "correlation_id": f"{endpoint_ref}-{i}",
            "endpoint_ref": endpoint_ref,
            "final_success": i < successes,
        }
        for i in range(n)
    ]


def _request(task_type: str = "code_generation") -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type=task_type,  # type: ignore[arg-type]
        prompt="x" * 100,
        emitted_at=datetime.now(tz=UTC),
    )


# --- build_roi_overlay (pure aggregation) --------------------------------------


def test_build_suppresses_tier_below_floor_with_enough_samples() -> None:
    rows = _roi_rows("local-coder", n=6, successes=0)
    overlay = build_roi_overlay(
        rows,
        task_type="code_generation",
        tier_of_endpoint=lambda _: "local",
        min_samples=5,
        success_floor=0.5,
    )
    assert overlay.is_suppressed("local")
    assert overlay.suppressed_tiers == frozenset({"local"})
    (signal,) = overlay.signals
    assert signal.sample_count == 6
    assert signal.success_rate == 0.0


def test_build_does_not_suppress_tier_above_floor() -> None:
    rows = _roi_rows("local-coder", n=6, successes=4)  # 0.667 >= 0.5
    overlay = build_roi_overlay(
        rows,
        task_type="code_generation",
        tier_of_endpoint=lambda _: "local",
        min_samples=5,
        success_floor=0.5,
    )
    assert not overlay.is_suppressed("local")
    assert overlay.suppressed_tiers == frozenset()


def test_build_does_not_suppress_on_thin_samples() -> None:
    rows = _roi_rows("local-coder", n=3, successes=0)  # below min_samples
    overlay = build_roi_overlay(
        rows,
        task_type="code_generation",
        tier_of_endpoint=lambda _: "local",
        min_samples=5,
        success_floor=0.5,
    )
    assert not overlay.is_suppressed("local")
    (signal,) = overlay.signals
    assert signal.sample_count == 3


def test_build_ignores_rows_with_unmapped_endpoint() -> None:
    rows = _roi_rows("mystery-backend", n=6, successes=0)
    overlay = build_roi_overlay(
        rows,
        task_type="code_generation",
        tier_of_endpoint=lambda _: None,
        min_samples=5,
        success_floor=0.5,
    )
    assert overlay.signals == ()
    assert overlay.suppressed_tiers == frozenset()


def test_build_signals_are_sorted_by_tier_name() -> None:
    rows = _roi_rows("cloud-glm", n=6, successes=0) + _roi_rows(
        "local-coder", n=6, successes=6
    )
    mapping = {"cloud-glm": "cheap_cloud", "local-coder": "local"}
    overlay = build_roi_overlay(
        rows,
        task_type="code_generation",
        tier_of_endpoint=lambda e: mapping.get(e),
        min_samples=5,
        success_floor=0.5,
    )
    assert [s.tier_name for s in overlay.signals] == ["cheap_cloud", "local"]
    assert overlay.suppressed_tiers == frozenset({"cheap_cloud"})


def test_build_handles_int_and_str_final_success() -> None:
    """asyncpg/psql surface bool as 0/1 or 't'/'f' — aggregation stays correct."""
    rows: list[dict[str, object]] = [
        {"endpoint_ref": "local-coder", "final_success": 0} for _ in range(3)
    ] + [{"endpoint_ref": "local-coder", "final_success": "f"} for _ in range(3)]
    overlay = build_roi_overlay(
        rows,
        task_type="code_generation",
        tier_of_endpoint=lambda _: "local",
        min_samples=5,
        success_floor=0.5,
    )
    (signal,) = overlay.signals
    assert signal.success_count == 0
    assert signal.suppressed


# --- resolve_roi_overlay (fail-open reader) ------------------------------------


def test_resolve_reads_rows_and_builds_overlay() -> None:
    db = InmemoryDatabaseAdapter()
    for row in _roi_rows("local-coder", n=6, successes=0):
        db.upsert(CONTEXT_ROI_TABLE, "correlation_id", row)
    overlay = resolve_roi_overlay(
        db,
        task_type="code_generation",
        tier_of_endpoint=lambda _: "local",
        min_samples=5,
        success_floor=0.5,
    )
    assert overlay is not None
    assert overlay.is_suppressed("local")


def test_resolve_empty_table_returns_overlay_with_no_suppression() -> None:
    db = InmemoryDatabaseAdapter()
    overlay = resolve_roi_overlay(
        db,
        task_type="code_generation",
        tier_of_endpoint=lambda _: "local",
    )
    assert overlay is not None
    assert overlay.suppressed_tiers == frozenset()


def test_resolve_fail_open_on_query_error() -> None:
    class _Boom:
        def query(self, table: str, filters: dict[str, object] | None = None):
            raise RuntimeError("projection unreachable")

    overlay = resolve_roi_overlay(
        _Boom(),
        task_type="code_generation",
        tier_of_endpoint=lambda _: "local",
    )
    assert overlay is None


def test_env_overrides_min_samples_and_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELEGATION_ROI_MIN_SAMPLES", "3")
    monkeypatch.setenv("DELEGATION_ROI_SUCCESS_FLOOR", "0.9")
    assert resolve_roi_min_samples() == 3
    assert resolve_roi_success_floor() == 0.9
    # A malformed / out-of-range override falls back to the default, never disables the gate.
    monkeypatch.setenv("DELEGATION_ROI_MIN_SAMPLES", "0")
    monkeypatch.setenv("DELEGATION_ROI_SUCCESS_FLOOR", "5")
    assert resolve_roi_min_samples() == 5  # DEFAULT_ROI_MIN_SAMPLES
    assert resolve_roi_success_floor() == 0.5  # DEFAULT_ROI_SUCCESS_FLOOR


# --- Pure reducer: the stored outcome CHANGES the decision ----------------------


@pytest.mark.usefixtures("code_gen_routable")
def test_first_eligible_tier_static_is_local() -> None:
    """Baseline: no overlay -> cheapest-first static tier (local)."""
    assert first_eligible_tier("code_generation") == "local"
    assert first_eligible_tier("code_generation", roi_overlay=None) == "local"


@pytest.mark.usefixtures("code_gen_routable")
def test_first_eligible_tier_roi_suppression_flips_to_cheap_cloud() -> None:
    """THE closed loop: a captured below-floor local ROI routes to cheap_cloud."""
    overlay = _suppress("local")
    assert first_eligible_tier("code_generation", roi_overlay=overlay) == "cheap_cloud"


@pytest.mark.usefixtures("code_gen_routable")
def test_first_eligible_tier_overlay_present_but_not_suppressed_stays_static() -> None:
    """An overlay whose local signal is above floor does not change the decision."""
    overlay = ModelRoutingRoiOverlay(
        task_type="code_generation",
        min_samples=5,
        success_floor=0.5,
        signals=(
            ModelTierRoiSignal(
                tier_name="local",
                sample_count=6,
                success_count=5,
                success_rate=0.833,
                suppressed=False,
            ),
        ),
    )
    assert first_eligible_tier("code_generation", roi_overlay=overlay) == "local"


@pytest.mark.usefixtures("code_gen_routable")
def test_first_eligible_tier_all_suppressed_failsafe_to_static() -> None:
    """If every routable tier is suppressed, fail-safe returns the static first tier."""
    overlay = _suppress("local", "cheap_cloud", "cheap_frontier", "claude")
    assert first_eligible_tier("code_generation", roi_overlay=overlay) == "local"


@pytest.mark.usefixtures("code_gen_routable")
def test_next_eligible_tier_static_from_local_is_cheap_cloud() -> None:
    assert (
        next_eligible_tier("local", frozenset(), task_type="code_generation")
        == "cheap_cloud"
    )


@pytest.mark.usefixtures("code_gen_routable")
def test_next_eligible_tier_roi_suppression_flips_to_cheap_frontier() -> None:
    """Escalation demotes a proven-failing cheap_cloud to cheap_frontier."""
    overlay = _suppress("cheap_cloud")
    assert (
        next_eligible_tier(
            "local", frozenset(), task_type="code_generation", roi_overlay=overlay
        )
        == "cheap_frontier"
    )


@pytest.mark.usefixtures("code_gen_routable")
def test_next_eligible_tier_failsafe_when_only_next_tier_suppressed() -> None:
    """Suppressing the only remaining routable next tier falls back to it (never dead-end)."""
    overlay = _suppress("cheap_frontier", "claude")
    # From cheap_cloud the only routable higher tiers are cheap_frontier/claude
    # (both suppressed) -> fail-safe second pass returns cheap_frontier.
    assert (
        next_eligible_tier(
            "cheap_cloud", frozenset(), task_type="code_generation", roi_overlay=overlay
        )
        == "cheap_frontier"
    )


@pytest.mark.usefixtures("code_gen_routable")
def test_delta_static_routes_local() -> None:
    decision = delta(_request())
    assert decision.tier_name == "local"


@pytest.mark.usefixtures("code_gen_routable")
def test_delta_roi_suppression_routes_cheap_cloud() -> None:
    """delta() honours the overlay: proven-failing local -> cheap_cloud decision."""
    decision = delta(_request(), roi_overlay=_suppress("local"))
    assert decision.tier_name == "cheap_cloud"
    assert "ROI-demoted past ['local']" in decision.rationale


@pytest.mark.usefixtures("code_gen_routable")
def test_delta_none_overlay_identical_to_static() -> None:
    """roi_overlay=None is byte-identical to the static decision (golden-safe)."""
    static = delta(_request())
    with_none = delta(_request(), roi_overlay=None)
    assert static.tier_name == with_none.tier_name == "local"
    assert "ROI-demoted" not in with_none.rationale


@pytest.mark.usefixtures("code_gen_routable")
def test_delta_all_suppressed_failsafe_to_static() -> None:
    decision = delta(
        _request(),
        roi_overlay=_suppress("local", "cheap_cloud", "cheap_frontier", "claude"),
    )
    assert decision.tier_name == "local"


# --- Port enforcement wire (onex delegate live path) ---------------------------


def test_port_default_reader_returns_none_without_roi_db() -> None:
    """Local default: no ROI projection wired -> fail-open None -> static routing."""
    port = LocalDelegationDispatchPort()
    assert port._roi_overlay_reader("code_generation") is None


def test_port_default_reader_reads_injected_roi_db() -> None:
    """When a projection DB carrying context_roi_scores is injected, the reader enforces."""
    db = InmemoryDatabaseAdapter()
    for row in _roi_rows("local-coder", n=6, successes=0):
        db.upsert(CONTEXT_ROI_TABLE, "correlation_id", row)
    port = LocalDelegationDispatchPort(roi_db=db)
    overlay = port._roi_overlay_reader("code_generation")
    assert overlay is not None
    # local-coder maps to the local tier via the routing authority.
    assert overlay.is_suppressed("local")


@pytest.mark.usefixtures("code_gen_routable")
def test_port_resolve_initial_backend_roi_flips_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enforcement wire: a suppressed local tier flips the resolved backend.

    ``resolve_delegation_backend`` is stubbed to echo the resolved backend_id/tier
    so this isolates the port's tier-selection change (the OMN-14001 wire) from
    the committed bifrost config the real resolver reads.
    """

    def _fake_resolve(task_type: str, *, backend_id: str | None = None, **_: object):
        return SimpleNamespace(
            backend_id=backend_id,
            tier=tier_for_backend(backend_id) if backend_id else "unknown",
        )

    monkeypatch.setattr(port_module, "resolve_delegation_backend", _fake_resolve)
    port = LocalDelegationDispatchPort()

    static = port._resolve_initial_backend("code_generation")
    assert static.backend_id == "local-coder"
    assert static.tier == "local"

    flipped = port._resolve_initial_backend(
        "code_generation", roi_overlay=_suppress("local")
    )
    assert flipped.backend_id == "cloud-glm"
    assert flipped.tier == "cheap_cloud"


# --- CAPSTONE: the closed loop end-to-end through canonical handlers ------------


@pytest.mark.usefixtures("code_gen_routable")
def test_learning_loop_capture_to_decision_end_to_end() -> None:
    """The FIRST closed platform learning loop, proven through canonical surfaces.

    CAPTURE (the canonical projection handler ``HandlerProjectionContextRoi`` writes
    the same ``context_roi_scores`` rows a live context-ROI run would) -> READ-BACK
    (``resolve_roi_overlay`` reads them through the ``DatabaseAdapter`` protocol) ->
    DECIDE (``first_eligible_tier`` routes the SAME request to a DIFFERENT tier).

    This is the operator's bar: a stored outcome ACTUALLY changes a live routing
    decision. The rows are materialised by the real projection handler (not a
    hand-built overlay), so the whole path — capture, store, read, decide — is
    exercised end-to-end.
    """
    from omnimarket.events.context_roi import (
        ModelAttemptReductionRow,
        ModelContextRoiRunResult,
    )
    from omnimarket.nodes.node_projection_context_roi.handlers.handler_projection_context_roi import (
        HandlerProjectionContextRoi,
    )

    db = InmemoryDatabaseAdapter()
    # Real captured outcomes: the LOCAL tier (endpoint_ref=local-coder) failed the
    # generation task on all 6 measured cells.
    rows = tuple(
        ModelAttemptReductionRow(
            run_id="run-omn14001",
            correlation_id=f"cell-{i}",
            task_id="fixed-task-1",
            endpoint_ref="local-coder",
            final_success=False,
        )
        for i in range(6)
    )
    result = ModelContextRoiRunResult(run_id="run-omn14001", rows=rows)

    # 1. CAPTURE — canonical projection handler materialises context_roi_scores.
    projection = HandlerProjectionContextRoi().project(result, db)
    assert projection.rows_upserted == 6

    # 2. BASELINE — with no read-back, static routing picks the cheapest tier: local.
    assert first_eligible_tier("code_generation") == "local"

    # 3. READ-BACK — the reader turns the captured rows into a tier-suppression overlay.
    overlay = resolve_roi_overlay(
        db,
        task_type="code_generation",
        tier_of_endpoint=tier_for_backend,
        min_samples=5,
        success_floor=0.5,
    )
    assert overlay is not None
    assert overlay.is_suppressed("local")

    # 4. DECISION CHANGES — the SAME request now routes to cheap_cloud because the
    #    stored outcome demoted the proven-failing local tier. The loop is closed.
    assert first_eligible_tier("code_generation", roi_overlay=overlay) == "cheap_cloud"


# --- LIVE wiring: the delegate path actually points the reader at the DB --------


def test_resolve_context_roi_db_none_without_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No OMNIDASH_ANALYTICS_DB_URL → None (fail-open, the common local case)."""
    from omnimarket.routing.roi_overlay import resolve_context_roi_db

    monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
    assert resolve_context_roi_db() is None


def test_resolve_context_roi_db_builds_lazy_adapter_with_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DSN set → a read adapter is returned WITHOUT connecting (lazy)."""
    from omnimarket.projection.postgres_read_database import PostgresReadDatabaseAdapter
    from omnimarket.routing.roi_overlay import resolve_context_roi_db

    monkeypatch.setenv(
        "OMNIDASH_ANALYTICS_DB_URL", "postgresql://u:p@127.0.0.1:1/omnidash_analytics"
    )
    db = resolve_context_roi_db()
    assert isinstance(db, PostgresReadDatabaseAdapter)


def test_port_selection_injects_roi_db_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SOLE live constructor wires the port's roi_db from the DSN env var.

    This is the OMN-14001 live-wiring: `select_delegation_dispatch_port(None)`
    (the bus-less `onex delegate` path) now builds a LocalDelegationDispatchPort
    whose ROI reader points at the real projection DB — so the loop consults ROI
    at runtime, not only in tests.
    """
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_selection import (
        select_delegation_dispatch_port,
    )
    from omnimarket.projection.postgres_read_database import PostgresReadDatabaseAdapter

    monkeypatch.setenv(
        "OMNIDASH_ANALYTICS_DB_URL", "postgresql://u:p@127.0.0.1:1/omnidash_analytics"
    )
    port = select_delegation_dispatch_port(None)
    assert isinstance(port._roi_db, PostgresReadDatabaseAdapter)

    monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
    port_off = select_delegation_dispatch_port(None)
    assert port_off._roi_db is None


def test_postgres_read_adapter_is_read_only_and_guards_identifiers() -> None:
    """upsert is unsupported (read-only); table identifiers are validated."""
    from omnimarket.projection.postgres_read_database import PostgresReadDatabaseAdapter

    adapter = PostgresReadDatabaseAdapter("postgresql://u:p@127.0.0.1:1/db")
    with pytest.raises(RuntimeError, match="read-only"):
        adapter.upsert("t", "id", {})
    with pytest.raises(ValueError, match="unsafe table identifier"):
        adapter._quote_ident("bad; DROP TABLE x")
    assert adapter._quote_ident("context_roi_scores") == '"context_roi_scores"'
