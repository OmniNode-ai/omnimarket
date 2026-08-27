# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15631 v1(a) — per-tenant delegation routing overlay.

Design-feasibility assessment (ticket comment 41f99997) shipped without the
`tenant`-schema RLS foundation (OMN-14894/OMN-15356): AC2 (DB-enforced
cross-tenant denial) is split into a follow-on ticket. This module proves
AC1, AC3 (the "no overlay -> platform default" half — the "provably
vendor-neutral platform_catalog row" half is out of v1(a) scope), AC4, AC5,
and AC6.

Test double: ``InmemoryDatabaseAdapter`` (``omnimarket.projection.
protocol_database``) — the SAME double the sibling ROI-overlay tests
(OMN-14001, ``test_roi_overlay_routing_omn14001.py``) already use for exactly
this class of proof. It satisfies ``ProtocolTenantOverlayReader``'s
``query(table, filters)`` structurally, identically to the real
``PostgresReadDatabaseAdapter``. AC1's falsifier rules out a test that
provisions a tenant by writing a YAML *file*; seeding this adapter via
``upsert()`` is a pure DATA write against the exact interface the routing
reducer's I/O boundary reads through — no file under ``src/`` or ``configs/``
is touched and no new process env is set to provision a tenant. A live
Postgres proof against the real migration 0001 table is the natural follow-up
once a reachable Postgres lane is available in this environment; this suite
is the executable proof available here today.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.inference.secret_store_resolver import (
    SecretResolutionError,
    resolve_tenant_scoped_api_key_async,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    TENANT_OVERLAY_TIER_NAME,
    delta,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing.tenant_overlay_resolver import (
    TENANT_OVERLAY_TABLE,
    ModelTenantRoutingOverlayBackend,
    resolve_tenant_overlay,
)

# --- Platform-default fixture (mirrors test_roi_overlay_routing_omn14001.py) ----

_BIFROST_ONE_TIER = textwrap.dedent(
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
    routing_rules:
      - rule_id: "c0ffee00-0011-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder]
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
def platform_default_routable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Point routing at a self-contained platform-default contract (AC3/AC4)."""
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_ONE_TIER)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()


def _request(
    task_type: str = "code_generation",
    tenant_id: str | None = None,
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type=task_type,  # type: ignore[arg-type]
        prompt="x" * 100,
        emitted_at=datetime.now(tz=UTC),
        tenant_id=tenant_id,
    )


def _seed_overlay_row(
    db: InmemoryDatabaseAdapter,
    *,
    tenant_id: str,
    task_type: str = "code_generation",
    backend_id: str = "tenant-own-provider",
    endpoint_url: str = "https://tenant.example.com/v1/chat/completions",
    model_name: str = "tenant-model-x",
    secret_ref: str | None = "tenant.acme.api_key",
    timeout_ms: int | None = None,
    max_tokens: int | None = None,
) -> None:
    """Provision a tenant overlay row via a pure DATA write (AC1 falsifier)."""
    row: dict[str, object] = {
        "tenant_id": tenant_id,
        "task_type": task_type,
        "backend_id": backend_id,
        "endpoint_url": endpoint_url,
        "model_name": model_name,
        "secret_ref": secret_ref,
        "timeout_ms": timeout_ms,
        "max_tokens": max_tokens,
    }
    db.upsert(TENANT_OVERLAY_TABLE, "tenant_id,task_type", row)


# --- AC1: two disjoint tenants + house tenant, pure data writes only -----------


def test_ac1_two_disjoint_tenants_resolve_their_own_overlay() -> None:
    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(
        db,
        tenant_id="acme-corp",
        backend_id="acme-bedrock",
        endpoint_url="https://bedrock.acme.example.com/v1/chat/completions",
        model_name="anthropic.claude-acme",
        secret_ref="tenant.acme-corp.bedrock.api_key",
    )
    _seed_overlay_row(
        db,
        tenant_id="widgets-inc",
        backend_id="widgets-openrouter",
        endpoint_url="https://openrouter.example.com/v1/chat/completions",
        model_name="widgets-preferred-model",
        secret_ref="tenant.widgets-inc.openrouter.api_key",
    )

    acme_overlay = resolve_tenant_overlay(
        db, tenant_id="acme-corp", task_type="code_generation"
    )
    widgets_overlay = resolve_tenant_overlay(
        db, tenant_id="widgets-inc", task_type="code_generation"
    )
    assert acme_overlay is not None
    assert widgets_overlay is not None

    acme_decision = delta(_request(tenant_id="acme-corp"), tenant_overlay=acme_overlay)
    widgets_decision = delta(
        _request(tenant_id="widgets-inc"), tenant_overlay=widgets_overlay
    )

    assert (
        acme_decision.endpoint_url
        == "https://bedrock.acme.example.com/v1/chat/completions"
    )
    assert acme_decision.selected_model == "anthropic.claude-acme"
    assert acme_decision.api_key_ref == "tenant.acme-corp.bedrock.api_key"
    assert acme_decision.selected_backend_ref == "acme-bedrock"

    assert (
        widgets_decision.endpoint_url
        == "https://openrouter.example.com/v1/chat/completions"
    )
    assert widgets_decision.selected_model == "widgets-preferred-model"
    assert widgets_decision.api_key_ref == "tenant.widgets-inc.openrouter.api_key"
    assert widgets_decision.selected_backend_ref == "widgets-openrouter"

    # Disjoint: neither tenant's decision carries the other's backend binding.
    assert acme_decision.endpoint_url != widgets_decision.endpoint_url
    assert acme_decision.selected_backend_id != widgets_decision.selected_backend_id


def test_ac1_house_tenant_never_queries_the_overlay_table() -> None:
    """The house tenant (tenant-zero) must not touch the overlay table at all."""
    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(db, tenant_id="acme-corp")

    assert (
        resolve_tenant_overlay(db, tenant_id=None, task_type="code_generation") is None
    )
    assert (
        resolve_tenant_overlay(
            db, tenant_id=HOUSE_TENANT_SLUG, task_type="code_generation"
        )
        is None
    )
    # The overlay table was seeded but never consulted for the house tenant —
    # confirmed indirectly: a lookup under a still-unseeded task_type for the
    # SAME real tenant correctly misses, proving the query path is live and
    # the house-tenant None above is a real short-circuit, not an accident.
    assert (
        resolve_tenant_overlay(db, tenant_id="acme-corp", task_type="research") is None
    )


@pytest.mark.usefixtures("platform_default_routable")
def test_ac3_no_overlay_row_resolves_platform_default() -> None:
    """T3: authenticated tenant, zero overlay rows -> platform default."""
    db = InmemoryDatabaseAdapter()  # empty — no rows for T3 at all
    t3_overlay = resolve_tenant_overlay(
        db, tenant_id="t3-no-overlay", task_type="code_generation"
    )
    assert t3_overlay is None

    decision = delta(_request(tenant_id="t3-no-overlay"), tenant_overlay=t3_overlay)
    # Endpoint comes from the fixture's bifrost contract; the selected model
    # id comes from the real (conftest-bound) routing_tiers.yaml tier ladder
    # for the "local" tier — routing STRUCTURE is untouched by this fixture.
    assert decision.endpoint_url == "http://local.test:8000/v1/chat/completions"
    assert decision.tier_name != TENANT_OVERLAY_TIER_NAME
    assert decision.selected_backend_ref == "local-coder"


# --- AC4: tenant-zero equivalence, proven by equivalence not assertion --------


@pytest.mark.usefixtures("platform_default_routable")
def test_ac4_tenant_zero_decision_is_byte_identical_with_and_without_overlay_wiring() -> (
    None
):
    """The pre-OMN-15631 call shape (no tenant_overlay kwarg) must be identical
    to the post-OMN-15631 call shape when tenant_overlay resolves to None.
    """
    request = _request(tenant_id=HOUSE_TENANT_SLUG)

    pre_omn15631_shape = delta(request)  # no tenant_overlay kwarg at all
    db = InmemoryDatabaseAdapter()
    resolved = resolve_tenant_overlay(
        db, tenant_id=request.tenant_id, task_type=request.task_type
    )
    post_omn15631_shape = delta(request, tenant_overlay=resolved)

    assert pre_omn15631_shape == post_omn15631_shape


# --- AC5: RED-first on the tenant dimension ------------------------------------


@pytest.mark.usefixtures("platform_default_routable")
def test_ac5_red_tenant_blindness_reproduced_when_overlay_not_threaded() -> None:
    """RED: reproduces the pre-fix defect — delta() ignores tenant_id entirely
    when no tenant_overlay is threaded in (exactly today's un-migrated call
    sites, and exactly what `delta()` did before this ticket for EVERY
    caller). Two different tenants, same task_type, same result: the tenant
    dimension provably does not exist on this path.
    """
    acme_request = _request(tenant_id="acme-corp")
    widgets_request = _request(tenant_id="widgets-inc")

    # Neither caller resolves/threads a tenant_overlay — the OMN-15631 default.
    acme_decision = delta(acme_request)
    widgets_decision = delta(widgets_request)

    assert acme_decision.endpoint_url == widgets_decision.endpoint_url
    assert acme_decision.selected_model == widgets_decision.selected_model
    assert acme_decision.model_dump(exclude={"correlation_id"}) == (
        widgets_decision.model_dump(exclude={"correlation_id"})
    )


@pytest.mark.usefixtures("platform_default_routable")
def test_ac5_green_tenant_overlay_inverts_the_red_case() -> None:
    """GREEN: the SAME two tenants, SAME task_type, now diverge once each
    tenant's own overlay is resolved and threaded — the tenant dimension now
    exists on this path. Direct inversion of the RED case above.
    """
    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(
        db,
        tenant_id="acme-corp",
        backend_id="acme-bedrock",
        endpoint_url="https://bedrock.acme.example.com/v1/chat/completions",
        model_name="acme-model",
    )
    _seed_overlay_row(
        db,
        tenant_id="widgets-inc",
        backend_id="widgets-openrouter",
        endpoint_url="https://openrouter.example.com/v1/chat/completions",
        model_name="widgets-model",
    )

    acme_request = _request(tenant_id="acme-corp")
    widgets_request = _request(tenant_id="widgets-inc")

    acme_decision = delta(
        acme_request,
        tenant_overlay=resolve_tenant_overlay(
            db, tenant_id="acme-corp", task_type="code_generation"
        ),
    )
    widgets_decision = delta(
        widgets_request,
        tenant_overlay=resolve_tenant_overlay(
            db, tenant_id="widgets-inc", task_type="code_generation"
        ),
    )

    assert acme_decision.endpoint_url != widgets_decision.endpoint_url
    assert acme_decision.selected_backend_ref != widgets_decision.selected_backend_ref
    # Both diverge from the platform default proven in the RED case.
    assert acme_decision.selected_backend_ref != "local-coder"
    assert widgets_decision.selected_backend_ref != "local-coder"


def test_delta_rejects_overlay_bound_to_a_different_request() -> None:
    """A caller must not be able to route one tenant/task through another overlay."""
    overlay = ModelTenantRoutingOverlayBackend(
        tenant_id="acme-corp",
        task_type="code_generation",
        backend_id="acme-bedrock",
        endpoint_url="https://bedrock.acme.example.com/v1/chat/completions",
        model_name="acme-model",
    )

    with pytest.raises(ValueError, match="tenant_overlay must match"):
        delta(_request(tenant_id="widgets-inc"), tenant_overlay=overlay)

    with pytest.raises(ValueError, match="tenant_overlay must match"):
        delta(
            _request(tenant_id="acme-corp", task_type="research"),
            tenant_overlay=overlay,
        )


# --- AC6: cross-boundary seam test, field-by-field -----------------------------


def test_ac6_overlay_row_to_routing_decision_field_seam() -> None:
    """Drives the REAL seam end-to-end: a DB row -> ModelTenantRoutingOverlayBackend
    -> ModelRoutingDecision, asserting every AC6-declared field individually
    (not just "it routed somewhere").
    """
    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(
        db,
        tenant_id="acme-corp",
        task_type="research",
        backend_id="acme-research-backend",
        endpoint_url="https://research.acme.example.com/v1/chat/completions",
        model_name="acme-research-model",
        secret_ref="tenant.acme-corp.research.api_key",
        timeout_ms=45000,
        max_tokens=32768,
    )

    overlay = resolve_tenant_overlay(db, tenant_id="acme-corp", task_type="research")
    assert overlay == ModelTenantRoutingOverlayBackend(
        tenant_id="acme-corp",
        task_type="research",
        backend_id="acme-research-backend",
        endpoint_url="https://research.acme.example.com/v1/chat/completions",
        model_name="acme-research-model",
        secret_ref="tenant.acme-corp.research.api_key",
        timeout_ms=45000,
        max_tokens=32768,
    )

    decision = delta(
        _request(task_type="research", tenant_id="acme-corp"), tenant_overlay=overlay
    )
    assert decision.task_type == "research"
    assert decision.selected_model == "acme-research-model"
    assert (
        decision.endpoint_url == "https://research.acme.example.com/v1/chat/completions"
    )
    assert decision.api_key_ref == "tenant.acme-corp.research.api_key"
    assert decision.timeout_ms == 45000
    assert decision.max_tokens == 32768
    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert decision.selected_backend_ref == "acme-research-backend"
    assert decision.extra_headers is None


def test_ac6_optional_fields_default_when_row_omits_them() -> None:
    """timeout_ms/max_tokens/secret_ref are the declared-optional AC6 fields."""
    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(
        db,
        tenant_id="acme-corp",
        secret_ref=None,
        timeout_ms=None,
        max_tokens=None,
    )
    overlay = resolve_tenant_overlay(
        db, tenant_id="acme-corp", task_type="code_generation"
    )
    assert overlay is not None
    assert overlay.secret_ref is None
    assert overlay.timeout_ms is None
    assert overlay.max_tokens is None

    decision = delta(_request(tenant_id="acme-corp"), tenant_overlay=overlay)
    assert decision.api_key_ref is None
    assert decision.timeout_ms == 30000  # ModelRoutingDecision's own default
    from omnimarket.models.delegation.wire.model_token_limits import (
        DELEGATION_MAX_TOKENS_HARD_LIMIT,
    )

    assert decision.max_tokens == DELEGATION_MAX_TOKENS_HARD_LIMIT


def _overlay_migration_set() -> str:
    """Every migration this node ships, concatenated in the order they apply.

    Asserting against a single file was wrong: a database receives the whole
    directory in lexical order, so a property is satisfied by the SET, not by
    one member of it. Pinning file 0001 in particular pushed the previous
    repair to edit 0001 in place after it had already been applied, which is
    the OMN-16705 defect -- bootstrap.sql then refused every subsequent
    forward-migration run on the .201 dev lane.
    """
    directory = (
        Path(__file__).parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_delegation_routing_reducer"
        / "migrations"
    )
    files = sorted(directory.glob("*.sql"))
    assert files, f"no migrations found under {directory}"
    return "\n".join(path.read_text(encoding="utf-8") for path in files)


def _overlay_migration_statements() -> str:
    """The same set with ``--`` comment lines removed.

    Every one of these files documents what it deliberately does NOT do
    ("No DROP, no recreate, no TRUNCATE"), so a bare substring search over the
    raw text finds the prohibition rather than a violation of it.
    """
    return "\n".join(
        line
        for line in _overlay_migration_set().splitlines()
        if not line.lstrip().startswith("--")
    )


def test_migration_reconciles_duplicate_overlay_bindings_without_row_loss() -> None:
    """Duplicate tenant/task rows are rekeyed explicitly, not silently dropped."""
    migrations = _overlay_migration_set()

    assert "__reconciled_duplicate_" in migrations
    assert "UPDATE delegation_routing_tenant_overlay AS overlay" in migrations
    # The load-bearing half: reconciliation may rename, never remove.
    statements = _overlay_migration_statements()
    assert "DELETE FROM" not in statements.upper()
    assert "TRUNCATE" not in statements.upper()

    # RESIDUAL, recorded rather than papered over (OMN-16705). 0001 de-duplicates
    # ids with `row_number() OVER (PARTITION BY id ...)`, which can hand the same
    # replacement id to rows from two different partitions. A later commit
    # changed it to a global rank -- but 0001 was already applied, so that edit
    # is exactly what bricked the dev lane and has been reverted. The condition
    # is unreachable on every path that matters (a fresh table's id is BIGSERIAL;
    # a database carrying 0001 in its ledger provably has no duplicates, because
    # 0001 ends by adding a PRIMARY KEY on id) and it fails LOUDLY rather than
    # silently when it is reachable, so it cannot be repaired additively: nothing
    # can run before 0001's own PRIMARY KEY step.
    assert "row_number() OVER (PARTITION BY id ORDER BY ctid)" in migrations


def test_migration_enforces_positive_optional_limits() -> None:
    migrations = _overlay_migration_set()

    assert "delegation_routing_tenant_overlay_timeout_ms_positive" in migrations
    assert "delegation_routing_tenant_overlay_max_tokens_positive" in migrations
    assert "CHECK (timeout_ms IS NULL OR timeout_ms > 0)" in migrations
    assert "CHECK (max_tokens IS NULL OR max_tokens > 0)" in migrations


# --- Missing secret ref fails fast, never falls back to a house key ------------


@pytest.mark.asyncio
async def test_missing_tenant_secret_ref_fails_fast_no_house_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    monkeypatch.delenv("tenant.acme-corp.definitely-unset.api_key", raising=False)
    # Even if a house key happens to be configured for a DIFFERENT provider
    # name, resolve_tenant_scoped_api_key_async must not consult it — there is
    # no env_var_fallback parameter to leak one in.
    monkeypatch.setenv("GEMINI_API_KEY", "house-key-must-not-leak")

    with pytest.raises(SecretResolutionError, match="Tenant 'acme-corp'"):
        await resolve_tenant_scoped_api_key_async(
            "tenant.acme-corp.definitely-unset.api_key",
            tenant_id="acme-corp",
        )


@pytest.mark.asyncio
async def test_unauthenticated_tenant_backend_resolves_none() -> None:
    assert (
        await resolve_tenant_scoped_api_key_async(None, tenant_id="acme-corp") is None
    )


# --- HandlerRoutingIntent bus-path wiring --------------------------------------


@pytest.mark.usefixtures("platform_default_routable")
def test_handler_routing_intent_threads_tenant_overlay() -> None:
    from omnibase_core.models.delegation.wire import ModelRoutingIntent

    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(db, tenant_id="acme-corp", backend_id="acme-bedrock")

    handler = HandlerRoutingIntent(tenant_overlay_db=db)
    decision = handler.handle(
        ModelRoutingIntent(payload=_request(tenant_id="acme-corp"))
    )
    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert decision.selected_backend_ref == "acme-bedrock"


@pytest.mark.usefixtures("platform_default_routable")
def test_handler_routing_intent_house_tenant_unaffected() -> None:
    from omnibase_core.models.delegation.wire import ModelRoutingIntent

    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(db, tenant_id="acme-corp", backend_id="acme-bedrock")

    handler = HandlerRoutingIntent(tenant_overlay_db=db)
    decision = handler.handle(
        ModelRoutingIntent(payload=_request(tenant_id=HOUSE_TENANT_SLUG))
    )
    assert decision.tier_name != TENANT_OVERLAY_TIER_NAME
    assert decision.selected_backend_ref == "local-coder"
