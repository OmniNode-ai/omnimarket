# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The credential readers find a tenant's rows whichever tenant form stored them (OMN-21819).

The tenant-credentials writer stores the tenant name today; the registry UUID is
coming (stage 2). Before that switch, every reader must accept both forms, or a
row stamped in the other form disappears from routing, the key pool, the review
gateway and revocation. The alias is taken only from one unique, validated
``tenant_registry_mirror`` row; a missing, ambiguous or disagreeing mirror leaves
each reader on its existing exact-string read. These tests pin, per reader:

- request name or UUID against a row stored by name or by UUID (all four);
- rows split across both forms;
- another tenant's row is never admitted, in either form;
- a missing or ambiguous mirror keeps the exact-string read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
    GATEWAY_PROVIDER,
    ClaudeReviewGateway,
    select_host_keys,
)
from omnimarket.nodes.node_delegation_routing_reducer.tenant_key_selection import (
    TENANT_CREDENTIALS_TABLE,
    _select_pool_secret_ref,
    _selected_key_model,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    OVERLAY_TABLE,
    apply_credential_revoked,
)
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
)
from omnimarket.routing.byok_provider_backends import BYOK_MODEL_UNRESOLVED

pytestmark = pytest.mark.unit

SLUG = "acme"
UUID_ = "6f2b1c3e-0d4a-4f5e-9a8b-7c6d5e4f3a2b"
OTHER_SLUG = "other"
OTHER_UUID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
PROVIDER = "gemini"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
REF_A = "cred_acme_" + "a" * 32
REF_B = "cred_acme_" + "b" * 32
REF_X = "cred_other_" + "c" * 32

MIRROR = [
    {"tenant_slug": SLUG, "tenant_uuid": UUID_},
    {"tenant_slug": OTHER_SLUG, "tenant_uuid": OTHER_UUID},
]


class FakeReader:
    """Equality-filter reader over in-memory tables, like the read adapter."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self.tables = tables

    def query(
        self, table: str, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        rows = self.tables.get(table, [])
        return [
            dict(r)
            for r in rows
            if all(r.get(k) == v for k, v in (filters or {}).items())
        ]


def cred(
    tenant: str,
    ref: str,
    *,
    models: list[str] | None = None,
    revoked: bool = False,
    expires: datetime | None = None,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant,
        "provider": PROVIDER,
        "api_key_ref": ref,
        "models": models or ["model-1"],
        "revoked_at": NOW if revoked else None,
        "expires_at": expires,
    }


def reader(
    creds: list[dict[str, Any]], mirror: list[dict[str, Any]] | None = None
) -> FakeReader:
    return FakeReader(
        {
            TENANT_CREDENTIALS_TABLE: creds,
            TENANT_REGISTRY_MIRROR_TABLE: MIRROR if mirror is None else mirror,
        }
    )


def model(db: FakeReader, tenant: str) -> str:
    return _selected_key_model(
        db,
        tenant_id=tenant,
        provider=PROVIDER,
        secret_ref=REF_A,
        overlay_model="overlay-model",
    )


def pool(db: FakeReader, tenant: str, overlay: str | None = None) -> str | None:
    return _select_pool_secret_ref(
        db,
        tenant_id=tenant,
        provider=PROVIDER,
        overlay_secret_ref=overlay,
        selector_key=None,
        now=NOW,
    )


@pytest.mark.parametrize("request_form", [SLUG, UUID_])
@pytest.mark.parametrize("stored_form", [SLUG, UUID_])
def test_selected_model_is_found_whichever_form_stored_or_asks(
    request_form: str, stored_form: str
) -> None:
    assert (
        model(reader([cred(stored_form, REF_A, models=["model-1"])]), request_form)
        == "model-1"
    )


def test_selected_model_never_reads_another_tenants_row() -> None:
    db = reader(
        [
            cred(OTHER_SLUG, REF_A, models=["theirs"]),
            cred(OTHER_UUID, REF_A, models=["theirs"]),
        ]
    )
    assert model(db, SLUG) != "theirs"
    assert model(db, UUID_) != "theirs"


@pytest.mark.parametrize(
    "mirror",
    [
        [],
        [
            {"tenant_slug": SLUG, "tenant_uuid": UUID_},
            {"tenant_slug": SLUG, "tenant_uuid": OTHER_UUID},
        ],
    ],
    ids=["missing", "ambiguous"],
)
def test_selected_model_keeps_the_exact_read_without_a_unique_mirror_pair(
    mirror: list[dict[str, Any]],
) -> None:
    db = reader([cred(UUID_, REF_A, models=["model-1"])], mirror=mirror)
    assert model(db, SLUG) != "model-1"
    assert model(db, UUID_) == "model-1"


@pytest.mark.parametrize("request_form", [SLUG, UUID_])
@pytest.mark.parametrize("stored_form", [SLUG, UUID_])
def test_pool_chooses_from_rows_in_either_form(
    request_form: str, stored_form: str
) -> None:
    assert pool(reader([cred(stored_form, REF_A)]), request_form) == REF_A


@pytest.mark.parametrize("request_form", [SLUG, UUID_])
def test_pool_spans_rows_split_across_both_forms(request_form: str) -> None:
    db = reader([cred(SLUG, REF_A, revoked=True), cred(UUID_, REF_B)])
    assert pool(db, request_form, overlay=REF_A) == REF_B
    db = reader(
        [cred(SLUG, REF_A, expires=NOW - timedelta(days=1)), cred(UUID_, REF_B)]
    )
    assert pool(db, request_form, overlay=REF_A) == REF_B


@pytest.mark.parametrize("request_form", [SLUG, UUID_])
def test_pool_with_every_key_dead_in_both_forms_fails_closed(request_form: str) -> None:
    db = reader(
        [
            cred(SLUG, REF_A, revoked=True),
            cred(UUID_, REF_B, expires=NOW - timedelta(days=1)),
        ]
    )
    assert pool(db, request_form, overlay=REF_A) is None


def test_pool_never_admits_another_tenants_key() -> None:
    db = reader(
        [
            cred(SLUG, REF_A, revoked=True),
            cred(OTHER_SLUG, REF_X),
            cred(OTHER_UUID, REF_X),
        ]
    )
    assert pool(db, SLUG, overlay=REF_A) is None
    assert pool(db, UUID_, overlay=REF_A) is None


def test_pool_keeps_the_exact_read_without_a_mirror_row() -> None:
    db = reader([cred(UUID_, REF_B)], mirror=[])
    assert pool(db, SLUG, overlay=REF_A) == REF_A
    assert pool(db, UUID_, overlay=REF_A) == REF_B


# --- review gateway: the query and its tenant filter move together ---------------

HOST = "mac-1"


def gw_key(
    tenant: str,
    ref: str,
    *,
    revoked: bool = False,
    host: str = HOST,
    expires: datetime | None = None,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant,
        "provider": GATEWAY_PROVIDER,
        "api_key_ref": ref,
        "name": ref,
        "bound_host": host,
        "revoked_at": NOW if revoked else None,
        "expires_at": expires,
    }


def gateway_refs(db: FakeReader, tenant: str) -> list[str]:
    fake_self = SimpleNamespace(
        _catalog=db,
        _request=SimpleNamespace(tenant_id=tenant, host=HOST),
        _set_aside=set(),
    )
    return [
        k.api_key_ref
        for k in ClaudeReviewGateway.candidate_keys(
            cast(ClaudeReviewGateway, fake_self), now=NOW
        )
    ]


@pytest.mark.parametrize("request_form", [SLUG, UUID_])
@pytest.mark.parametrize("stored_form", [SLUG, UUID_])
def test_gateway_spends_the_tenants_key_whichever_form_stored_it(
    request_form: str, stored_form: str
) -> None:
    assert gateway_refs(reader([gw_key(stored_form, REF_A)]), request_form) == [REF_A]


@pytest.mark.parametrize("request_form", [SLUG, UUID_])
def test_gateway_keeps_revocation_host_and_tenant_limits_across_forms(
    request_form: str,
) -> None:
    db = reader(
        [
            gw_key(SLUG, REF_A, revoked=True),
            gw_key(UUID_, REF_B),
            gw_key(UUID_, REF_A + "x", host="other"),
            gw_key(OTHER_SLUG, REF_X),
            gw_key(OTHER_UUID, REF_X),
        ]
    )
    assert gateway_refs(db, request_form) == [REF_B]


def test_gateway_keeps_the_exact_read_without_a_mirror_row() -> None:
    db = reader([gw_key(UUID_, REF_B)], mirror=[])
    assert gateway_refs(db, SLUG) == []
    assert gateway_refs(db, UUID_) == [REF_B]


# --- revocation: the revoked key's route is blanked in whichever form holds it ---


class FakeStore(FakeReader):
    """Reads plus attested upserts, recorded; neither write fast path."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__(tables)
        self.upserts: list[tuple[str, dict[str, Any]]] = []

        self.scopes: list[str | None] = []

    def upsert_returning(
        self, table: str, conflict: str, row: dict[str, Any], **kw: Any
    ) -> dict[str, Any]:
        self.upserts.append((table, row))
        if table == OVERLAY_TABLE:
            self.scopes.append(kw.get("tenant"))
        return row


def route(tenant: str, ref: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant,
        "task_type": "code",
        "backend_id": "b",
        "provider": PROVIDER,
        "endpoint_url": "u",
        "model_name": "m",
        "timeout_ms": 1,
        "max_tokens": 1,
        "secret_ref": ref,
    }


def blanked_route_tenants(store: FakeStore) -> list[str]:
    return sorted(
        r["tenant_id"]
        for t, r in store.upserts
        if t == OVERLAY_TABLE and r.get("secret_ref") is None
    )


@pytest.mark.parametrize("request_form", [SLUG, UUID_])
@pytest.mark.parametrize("stored_form", [SLUG, UUID_])
def test_revocation_blanks_the_route_whichever_form_stored_it(
    request_form: str, stored_form: str
) -> None:
    store = FakeStore(
        {
            OVERLAY_TABLE: [route(stored_form, REF_A)],
            TENANT_CREDENTIALS_TABLE: [],
            TENANT_REGISTRY_MIRROR_TABLE: MIRROR,
        }
    )
    apply_credential_revoked(
        {"api_key_ref": REF_A, "tenant_id": request_form}, cast(Any, store)
    )
    assert blanked_route_tenants(store) == [stored_form]


def test_revocation_never_touches_another_tenants_route() -> None:
    store = FakeStore(
        {
            OVERLAY_TABLE: [route(OTHER_SLUG, REF_A), route(OTHER_UUID, REF_A)],
            TENANT_CREDENTIALS_TABLE: [],
            TENANT_REGISTRY_MIRROR_TABLE: MIRROR,
        }
    )
    apply_credential_revoked(
        {"api_key_ref": REF_A, "tenant_id": SLUG}, cast(Any, store)
    )
    assert blanked_route_tenants(store) == []


def test_revocation_keeps_the_exact_read_without_a_mirror_row() -> None:
    store = FakeStore(
        {
            OVERLAY_TABLE: [route(UUID_, REF_A)],
            TENANT_CREDENTIALS_TABLE: [],
            TENANT_REGISTRY_MIRROR_TABLE: [],
        }
    )
    apply_credential_revoked(
        {"api_key_ref": REF_A, "tenant_id": SLUG}, cast(Any, store)
    )
    assert blanked_route_tenants(store) == []


# --- mirror controls, per reader: only one unique, agreeing, valid row aliases ---


class MirrorAs(FakeStore):
    """The mirror answers as given (or raises), whatever the filter asked."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]], mirror: object) -> None:
        super().__init__(tables)
        self.mirror = mirror

    def query(
        self, table: str, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if table != TENANT_REGISTRY_MIRROR_TABLE:
            return super().query(table, filters)
        if isinstance(self.mirror, Exception):
            raise self.mirror
        return [dict(r) for r in cast(list[dict[str, Any]], self.mirror)]


MIRROR_CONTROLS = {
    "missing": [],
    "ambiguous": [
        {"tenant_slug": SLUG, "tenant_uuid": UUID_},
        {"tenant_slug": SLUG, "tenant_uuid": OTHER_UUID},
    ],
    "disagreeing": [{"tenant_slug": OTHER_SLUG, "tenant_uuid": UUID_}],
    "malformed-uuid": [{"tenant_slug": SLUG, "tenant_uuid": "not-a-uuid"}],
    "malformed-slug": [{"tenant_slug": "Not A Slug!", "tenant_uuid": UUID_}],
    "raising": RuntimeError("mirror unavailable"),
}


def controlled(
    mirror: object,
    creds: list[dict[str, Any]],
    routes: list[dict[str, Any]] | None = None,
) -> MirrorAs:
    return MirrorAs(
        {TENANT_CREDENTIALS_TABLE: creds, OVERLAY_TABLE: routes or []}, mirror
    )


@pytest.mark.parametrize("name", list(MIRROR_CONTROLS))
def test_selected_model_keeps_the_exact_read_under_every_mirror_control(
    name: str,
) -> None:
    db = controlled(MIRROR_CONTROLS[name], [cred(UUID_, REF_A, models=["model-1"])])
    # The existing fail-closed sentinel for a tenant key it cannot read.
    assert model(db, SLUG) == BYOK_MODEL_UNRESOLVED
    assert model(db, UUID_) == "model-1"


@pytest.mark.parametrize("name", list(MIRROR_CONTROLS))
def test_pool_keeps_the_exact_read_under_every_mirror_control(name: str) -> None:
    db = controlled(MIRROR_CONTROLS[name], [cred(UUID_, REF_B)])
    assert pool(db, SLUG, overlay=REF_A) == REF_A
    assert pool(db, UUID_, overlay=REF_A) == REF_B


@pytest.mark.parametrize("name", list(MIRROR_CONTROLS))
def test_gateway_keeps_the_exact_read_under_every_mirror_control(name: str) -> None:
    db = controlled(MIRROR_CONTROLS[name], [gw_key(UUID_, REF_B)])
    assert gateway_refs(cast(FakeReader, db), SLUG) == []
    assert gateway_refs(cast(FakeReader, db), UUID_) == [REF_B]


@pytest.mark.parametrize("name", list(MIRROR_CONTROLS))
def test_revocation_keeps_the_exact_read_under_every_mirror_control(
    name: str,
) -> None:
    store = controlled(MIRROR_CONTROLS[name], [], routes=[route(UUID_, REF_A)])
    apply_credential_revoked(
        {"api_key_ref": REF_A, "tenant_id": SLUG}, cast(Any, store)
    )
    assert blanked_route_tenants(store) == []
    store = controlled(MIRROR_CONTROLS[name], [], routes=[route(UUID_, REF_A)])
    apply_credential_revoked(
        {"api_key_ref": REF_A, "tenant_id": UUID_}, cast(Any, store)
    )
    assert blanked_route_tenants(store) == [UUID_]


# --- mixed forms: both stored forms at once ---


def test_selected_model_prefers_the_row_in_the_requested_form() -> None:
    db = reader(
        [
            cred(SLUG, REF_A, models=["by-name"]),
            cred(UUID_, REF_A, models=["by-uuid"]),
        ]
    )
    assert model(db, SLUG) == "by-name"
    assert model(db, UUID_) == "by-uuid"


@pytest.mark.parametrize("request_form", [SLUG, UUID_])
def test_revocation_blanks_the_routes_in_both_forms(request_form: str) -> None:
    store = FakeStore(
        {
            OVERLAY_TABLE: [route(SLUG, REF_A), route(UUID_, REF_A)],
            TENANT_CREDENTIALS_TABLE: [],
            TENANT_REGISTRY_MIRROR_TABLE: MIRROR,
        }
    )
    apply_credential_revoked(
        {"api_key_ref": REF_A, "tenant_id": request_form}, cast(Any, store)
    )
    assert blanked_route_tenants(store) == sorted([SLUG, UUID_])


@pytest.mark.parametrize("request_form", [SLUG, UUID_])
@pytest.mark.parametrize("stored_form", [SLUG, UUID_])
def test_revocation_scopes_each_route_write_to_the_route_s_own_tenant(
    request_form: str, stored_form: str
) -> None:
    store = FakeStore(
        {
            OVERLAY_TABLE: [route(stored_form, REF_A)],
            TENANT_CREDENTIALS_TABLE: [],
            TENANT_REGISTRY_MIRROR_TABLE: MIRROR,
        }
    )
    apply_credential_revoked(
        {"api_key_ref": REF_A, "tenant_id": request_form}, cast(Any, store)
    )
    assert store.scopes == [stored_form]
    (blanked,) = [r for t, r in store.upserts if t == OVERLAY_TABLE]
    assert {k: v for k, v in blanked.items() if k != "secret_ref"} == {
        k: v for k, v in route(stored_form, REF_A).items() if k != "secret_ref"
    }


@pytest.mark.parametrize("request_form", [SLUG, UUID_])
def test_gateway_drops_an_expired_key_stored_in_the_other_form(
    request_form: str,
) -> None:
    other = UUID_ if request_form == SLUG else SLUG
    db = reader(
        [
            gw_key(other, REF_A, expires=NOW - timedelta(days=1)),
            gw_key(other, REF_B, expires=NOW + timedelta(days=1)),
        ]
    )
    assert gateway_refs(db, request_form) == [REF_B]


def test_gateway_lists_a_key_stored_in_both_forms_once() -> None:
    db = reader([gw_key(SLUG, REF_A), gw_key(UUID_, REF_A)])
    assert gateway_refs(db, SLUG) == [REF_A]
    assert gateway_refs(db, UUID_) == [REF_A]


@pytest.mark.parametrize("request_form", [SLUG, UUID_])
def test_gateway_filter_keeps_another_tenants_rows_out_of_the_alias(
    request_form: str,
) -> None:
    # The filter on its own, as a widened catalog read would reach it: rows of
    # both tenants in both forms; only this tenant's forms may pass.
    rows = [
        gw_key(SLUG, REF_A),
        gw_key(UUID_, REF_B),
        gw_key(OTHER_SLUG, REF_X),
        gw_key(OTHER_UUID, REF_X + "y"),
    ]
    keys = select_host_keys(
        rows, tenant_id=request_form, host=HOST, now=NOW, tenant_forms=(SLUG, UUID_)
    )
    assert sorted(k.api_key_ref for k in keys) == sorted([REF_A, REF_B])
    exact = select_host_keys(rows, tenant_id=request_form, host=HOST, now=NOW)
    assert [k.api_key_ref for k in exact] == [REF_A if request_form == SLUG else REF_B]
