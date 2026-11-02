# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-key choices must survive pool selection and a losing global CAS.

Run on the lab: SQLite cases use real private files, never a shared database.
Failure modes are two keys sharing a provider, stale/missing global versions,
an error between route and catalogue writes, unrecoverable legacy choices,
malformed choices, and revoked/foreign keys. No provider call is made.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.handlers.routing.byok_model_discovery import BYOK_MODEL_NOT_CHOSEN
from omnimarket.handlers.routing.byok_provider_backends import (
    BYOK_MODEL_UNRESOLVED,
    resolve_byok_provider_backend,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    CREDENTIALS_TABLE,
    OVERLAY_TABLE,
    apply_credential_registered,
    apply_credential_revoked,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.routing.tenant_overlay_resolver import resolve_tenant_overlay

pytestmark = pytest.mark.integration
TENANT = "omn21217-private-test"
A = f"cred_{TENANT}_openrouter_{'a' * 32}"
B = f"cred_{TENANT}_openrouter_{'b' * 32}"


@pytest.fixture(params=("memory", "sqlite"))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "memory":
        return InmemoryDatabaseAdapter()
    return SqliteDatabaseAdapter(tmp_path / "per-key.sqlite")


def _register(db: Any, ref: str, models: list[str], **extra: Any) -> bool:
    return apply_credential_registered(
        {
            "tenant_id": TENANT,
            "provider": "openrouter",
            "name": ref,
            "api_key_ref": ref,
            "models": models,
            **extra,
        },
        db,
    )


def _catalogue(db: Any, ref: str) -> dict[str, Any]:
    rows = db.query(CREDENTIALS_TABLE, {"tenant_id": TENANT, "api_key_ref": ref})
    assert len(rows) == 1
    return rows[0]


def _overlay(db: Any) -> dict[str, Any]:
    rows = db.query(OVERLAY_TABLE, {"tenant_id": TENANT, "task_type": "*"})
    assert len(rows) == 1
    return rows[0]


def _selected(db: Any) -> dict[str, Any]:
    routes = {}
    for number in range(128):
        route = resolve_tenant_overlay(
            db,
            tenant_id=TENANT,
            task_type="code_generation",
            selector_key=f"two-key-{number}",
        )
        assert route is not None
        routes[route.secret_ref] = route
        if routes.keys() == {A, B}:
            return routes
    pytest.fail(f"positive control did not select both live keys: {routes.keys()}")


def test_each_pooled_key_keeps_its_own_ordered_choices(store: Any) -> None:
    chosen = {
        A: ["vendor/a-first", "vendor/a-next"],
        B: ["vendor/b-first", "vendor/b-next"],
    }
    for ref, models in chosen.items():
        assert _register(store, ref, models)
    selected = _selected(store)
    for ref, models in chosen.items():
        assert _catalogue(store, ref).get("models") == models
        assert selected[ref].model_name == models[0]


def test_one_global_cas_wins_without_clobbering_the_other_key(store: Any) -> None:
    _register(store, A, ["a-original", "a-next"])
    _register(store, B, ["b-original", "b-next"])
    before = _overlay(store)
    assert before["version"] == 2
    proposals = (["a-winner-one", "a-tail-one"], ["a-winner-two", "a-tail-two"])
    ready = Barrier(2, timeout=10)

    def replace(models: list[str]) -> bool:
        ready.wait()
        return _register(store, A, models, expected_version=2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        acknowledgements = list(pool.map(replace, proposals))
    # These are processed/ACK results, not customer CAS results.
    assert acknowledgements == [True, True]
    winner = _overlay(store)
    assert winner["version"] == 3
    assert winner["models"] in proposals
    assert _catalogue(store, A).get("models") == winner["models"]
    assert _catalogue(store, B).get("models") == ["b-original", "b-next"]
    _register(store, A, ["stale-should-not-publish"], expected_version=2)
    assert _overlay(store) == winner
    assert _catalogue(store, A).get("models") == winner["models"]


def test_missing_cas_row_does_not_publish_choices_and_legacy_overwrite_still_works(
    store: Any,
) -> None:
    assert _register(store, A, ["must-not-publish"], expected_version=1)
    assert store.query(OVERLAY_TABLE, {"tenant_id": TENANT}) == []
    assert _catalogue(store, A).get("models") is None
    assert _register(store, A, ["first", "second"])
    assert apply_credential_registered(
        {
            "tenant_id": TENANT,
            "provider": "openrouter",
            "name": "legacy",
            "api_key_ref": A,
            "metadata": {"model": "one-model-alias"},
        },
        store,
    )
    assert _overlay(store)["version"] == 2
    assert _catalogue(store, A).get("models") == ["one-model-alias"]


def test_revoked_key_is_not_revived_and_missing_key_posture_is_distinct(
    store: Any,
) -> None:
    apply_credential_revoked({"tenant_id": TENANT, "api_key_ref": A}, store)
    _register(store, A, ["never-live"])
    assert _catalogue(store, A)["revoked_at"] is not None
    assert _catalogue(store, A).get("models") is None
    assert store.query(OVERLAY_TABLE, {"tenant_id": TENANT}) == []
    _register(store, B, ["b-only"])
    apply_credential_revoked({"tenant_id": TENANT, "api_key_ref": B}, store)
    route = resolve_tenant_overlay(store, tenant_id=TENANT, task_type="code_generation")
    assert route is not None
    assert route.secret_ref is None


def test_foreign_tenant_choices_never_enter_the_pool(store: Any) -> None:
    _register(store, A, ["own-model"])
    apply_credential_registered(
        {
            "tenant_id": "another-tenant",
            "provider": "openrouter",
            "name": "foreign",
            "api_key_ref": B,
            "models": ["foreign-model"],
        },
        store,
    )
    for number in range(16):
        route = resolve_tenant_overlay(
            store,
            tenant_id=TENANT,
            task_type="code_generation",
            selector_key=str(number),
        )
        assert route is not None
        assert route.secret_ref == A
        assert route.model_name == "own-model"
    assert _catalogue(store, A).get("models") == ["own-model"]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_real_sqlite_legacy_backfill_recovers_only_exact_unambiguous_owner(
    tmp_path: Path, ambiguous: bool
) -> None:
    path = tmp_path / "legacy.sqlite"
    # A pre-feature store really lacks a catalogue models column. Two catalogue
    # refs share a provider, but only A has recorded choices on its own overlay.
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE tenant_inference_credentials (
                api_key_ref TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT,
                provider TEXT, created_at TEXT NOT NULL, revoked_at TEXT);
            CREATE TABLE delegation_routing_tenant_overlay (
                tenant_id TEXT NOT NULL, task_type TEXT NOT NULL, backend_id TEXT NOT NULL,
                provider TEXT, endpoint_url TEXT NOT NULL, model_name TEXT NOT NULL,
                models TEXT, secret_ref TEXT, timeout_ms INTEGER, max_tokens INTEGER,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, task_type));
        """)
        for ref in (A, B):
            conn.execute(
                "INSERT INTO tenant_inference_credentials VALUES (?, ?, 'legacy', 'openrouter', '2026-01-01', NULL)",
                (ref, TENANT),
            )
        backend = resolve_byok_provider_backend("openrouter")
        assert backend is not None
        for task, models in [("*", ["a-first", "a-next"])] + (
            [("code_generation", ["conflicting-choice"])] if ambiguous else []
        ):
            conn.execute(
                "INSERT INTO delegation_routing_tenant_overlay VALUES (?, ?, ?, 'openrouter', ?, ?, ?, ?, NULL, NULL, '2026-01-01', '2026-01-01')",
                (
                    TENANT,
                    task,
                    backend.backend_id,
                    backend.endpoint_url,
                    models[0],
                    json.dumps(models),
                    A,
                ),
            )
    db = SqliteDatabaseAdapter(path)
    assert _catalogue(db, A).get("models") == (
        None if ambiguous else ["a-first", "a-next"]
    )
    assert _catalogue(db, B).get("models") is None
    routes = _selected(db)
    assert routes[B].model_name == BYOK_MODEL_UNRESOLVED
    assert routes[A].model_name == (BYOK_MODEL_UNRESOLVED if ambiguous else "a-first")


def test_real_sqlite_error_between_writes_rolls_back_route_and_choices(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollback.sqlite"
    db = SqliteDatabaseAdapter(path)
    _register(db, A, ["before", "before-tail"])
    # Arm the real database fault even before production has added the column.
    # The independent fresh-schema test above still requires production storage.
    with sqlite3.connect(path) as conn:
        columns = {
            row[1] for row in conn.execute(f"PRAGMA table_info({CREDENTIALS_TABLE})")
        }
        if "models" not in columns:
            conn.execute(f"ALTER TABLE {CREDENTIALS_TABLE} ADD COLUMN models TEXT")
            conn.execute(
                f"UPDATE {CREDENTIALS_TABLE} SET models = ?",
                (json.dumps(["before", "before-tail"]),),
            )
        conn.execute(
            f"CREATE TRIGGER refuse_choices BEFORE UPDATE OF models ON {CREDENTIALS_TABLE} BEGIN SELECT RAISE(ABORT, 'chosen-model-write-fault'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="chosen-model-write-fault"):
            conn.execute(
                f"UPDATE {CREDENTIALS_TABLE} SET models = '[]' WHERE api_key_ref = ?",
                (A,),
            )
    before_route = _overlay(db)
    before_key = _catalogue(db, A)
    with pytest.raises(sqlite3.IntegrityError, match="chosen-model-write-fault"):
        _register(db, A, ["after", "after-tail"], expected_version=1)
    assert _overlay(db) == before_route
    assert _catalogue(db, A).get("models") == before_key.get("models")


@pytest.mark.parametrize(
    "bad", [None, [], [""], ["duplicate", "duplicate"], "other-key-model", [2]]
)
def test_unknown_or_malformed_selected_key_choices_fail_closed(
    store: Any, bad: Any
) -> None:
    _register(store, A, ["a-original"])
    _register(store, B, ["b-original"])
    store.upsert_returning(
        CREDENTIALS_TABLE, "api_key_ref", {**_catalogue(store, A), "models": bad}
    )
    routes = _selected(store)
    assert routes[B].model_name == "b-original"  # healthy control keeps pool enabled
    assert routes[A].secret_ref == A
    assert routes[A].model_name == BYOK_MODEL_UNRESOLVED


@pytest.mark.parametrize(
    "provider", ["openrouter", "gemini", "openai", "anthropic", "glm"]
)
def test_legacy_unknown_choices_reach_existing_refusal_before_transport(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = InmemoryDatabaseAdapter()
    backend = resolve_byok_provider_backend(provider)
    assert backend is not None
    ref = f"cred_{TENANT}_{provider}_{'a' * 32}"
    apply_credential_registered(
        {
            "tenant_id": TENANT,
            "provider": provider,
            "name": "known",
            "api_key_ref": ref,
            "models": ["owner-chosen"],
        },
        db,
    )
    handler = effect.HandlerLlmDelegationCall()
    calls: list[str] = []

    def transport_control(request: Any, endpoint_url: str, event_publisher: Any) -> Any:
        calls.append(request.model_id)
        return handler._failure_result(
            request, EnumDelegationFailureClass.MODEL_UNAVAILABLE, "transport-control"
        )

    def forbid_discovery(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("unknown choices must not invoke discovery or provider transport")

    monkeypatch.setattr(handler, "_execute_call_once", transport_control)
    monkeypatch.setattr(handler, "_resolve_byok_model", forbid_discovery)
    monkeypatch.setattr(effect, "resolve_local_byok_model_chooser", lambda _ref: None)

    def execute() -> Any:
        route = resolve_tenant_overlay(
            db, tenant_id=TENANT, task_type="code_generation"
        )
        assert route is not None
        assert route.secret_ref == ref
        request = ModelLlmDelegationCallRequest(
            request_id=str(uuid4()),
            correlation_id=str(uuid4()),
            causation_id=str(uuid4()),
            model_id=route.model_name,
            endpoint_ref=route.endpoint_url,
            prompt="synthetic",
            prompt_hash="0" * 64,
            timeout_seconds=5,
            secret_ref=route.secret_ref,
            api_key_env=None,
        )
        return handler._execute_call_attempts(request, route.endpoint_url, None)

    assert execute().error_message == "transport-control"
    assert calls == ["owner-chosen"]  # instrument can reach the lower call boundary
    db.upsert_returning(
        CREDENTIALS_TABLE, "api_key_ref", {"api_key_ref": ref, "models": None}
    )
    result = execute()
    assert not result.success
    assert result.failure_class is EnumDelegationFailureClass.PROVIDER_MODEL_NOT_FOUND
    assert BYOK_MODEL_NOT_CHOSEN in (result.error_message or "")
    assert calls == ["owner-chosen"]
