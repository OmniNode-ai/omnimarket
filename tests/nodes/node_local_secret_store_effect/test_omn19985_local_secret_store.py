# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A local provider key becomes credential events that never carry the key.

``onex secret set`` stored a key and told nothing else, so no Credentials page
could list it. ``node_local_secret_store_effect`` stores the key in the local
store as before and returns the metadata events the local runtime folds into
``tenant_inference_credentials``.

Each test names the failure it exists to catch:

* H1  the registered event's bytes carry the value, or its fingerprint is not
      ``sha256(value)[:8]``, or it is not keyed by the freshly minted route ref;
* H2  a re-set replaces the route key but leaves the replaced ref live, so the
      page shows two live keys for one provider;
* H3  delete returns no revoke, or one for a ref other than the live one;
* H4  a secret that is not a provider key (no ``llm.<provider>.<field>`` ref)
      produces an event, putting a non-provider secret's name on the page;
* H5  a refusal (already stored, nothing to delete, no value) prints the value
      or still emits an event;
* R1  register then revoke, folded, does not end revoked with its fingerprint;
* R2  the same two events in the other order end anywhere else;
* R3  a duplicate delivery of the registered event changes the row;
* R4  a delete then set of one ref name resurrects the first row instead of
      writing a second, live one;
* R5  a re-set (``--force``) leaves two live keys for one provider.

Every store is a SQLite file under pytest's ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import (
    LOCAL_INSTALL_TENANT_ID,
)
from omnimarket.nodes.node_local_secret_store_effect.handlers.handler_local_secret_store import (
    HandlerLocalSecretStore,
    LocalSecretStoreRefusedError,
)
from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_request import (
    ModelLocalSecretRequest,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    CREDENTIALS_TABLE,
    apply_credential_registered,
    apply_credential_revoked,
)
from omnimarket.projection.credential_publisher import (
    ModelCredentialRegisteredEvent,
    ModelCredentialRevokedEvent,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_REF = "llm.openrouter.api_key"
_PLANTED = "sk-or-v1-planted-omn19985-0123456789abcdef"
_SECOND = "sk-or-v1-second-omn19985-fedcba9876543210"
_SET_AT = datetime(2026, 10, 7, 7, 30, tzinfo=UTC)
_ROUTE_REF = re.compile(r"^cred_localinstall_openrouter_[0-9a-f]{32}$")


@pytest.fixture
def store_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    return db_path


def _handler() -> HandlerLocalSecretStore:
    return HandlerLocalSecretStore(now=lambda: _SET_AT)


def _set(
    value: str, *, ref: str = _REF, force: bool = False
) -> ModelLocalSecretRequest:
    return ModelLocalSecretRequest(
        operation="set", secret_ref=ref, value=SecretStr(value), force=force
    )


def _delete(ref: str = _REF) -> ModelLocalSecretRequest:
    return ModelLocalSecretRequest(operation="delete", secret_ref=ref)


def _fold(
    events: tuple[ModelCredentialRegisteredEvent | ModelCredentialRevokedEvent, ...],
    db: SqliteDatabaseAdapter,
) -> None:
    for event in events:
        payload = event.model_dump(mode="json")
        if isinstance(event, ModelCredentialRegisteredEvent):
            apply_credential_registered(payload, db)
        else:
            apply_credential_revoked(payload, db)


def _rows(db: SqliteDatabaseAdapter) -> list[dict[str, object]]:
    rows = db.query(CREDENTIALS_TABLE, {"tenant_id": LOCAL_INSTALL_TENANT_ID})
    return sorted(rows, key=lambda row: str(row["api_key_ref"]))


def test_h1_registered_event_carries_metadata_and_never_the_value(
    store_path: Path,
) -> None:
    result = _handler().handle(_set(_PLANTED))

    [event] = result.events
    assert isinstance(event, ModelCredentialRegisteredEvent)
    assert _PLANTED not in event.model_dump_json()
    assert _PLANTED.encode() not in event.model_dump_json().encode()
    assert event.fingerprint == hashlib.sha256(_PLANTED.encode()).hexdigest()[:8]
    assert _ROUTE_REF.fullmatch(event.api_key_ref)
    assert event.api_key_ref == result.route_ref
    assert event.tenant_id == LOCAL_INSTALL_TENANT_ID
    assert event.provider == "openrouter"
    assert event.name == _REF
    assert event.set_at == _SET_AT
    assert _PLANTED not in result.model_dump_json()


def test_h2_a_re_set_revokes_the_route_ref_it_replaces(store_path: Path) -> None:
    first = _handler().handle(_set(_PLANTED))
    second = _handler().handle(_set(_SECOND, force=True))

    revoked, registered = second.events
    assert isinstance(revoked, ModelCredentialRevokedEvent)
    assert isinstance(registered, ModelCredentialRegisteredEvent)
    assert revoked.api_key_ref == first.route_ref
    assert registered.api_key_ref == second.route_ref != first.route_ref
    assert registered.fingerprint == hashlib.sha256(_SECOND.encode()).hexdigest()[:8]


def test_h3_delete_returns_the_mirror_revoke_of_the_live_ref(store_path: Path) -> None:
    registered = _handler().handle(_set(_PLANTED))
    deleted = _handler().handle(_delete())

    [revoked] = deleted.events
    assert isinstance(revoked, ModelCredentialRevokedEvent)
    assert revoked.api_key_ref == registered.route_ref
    assert revoked.tenant_id == LOCAL_INSTALL_TENANT_ID
    assert deleted.route_withdrawn is True


def test_h4_a_secret_that_is_not_a_provider_key_emits_nothing(store_path: Path) -> None:
    stored = _handler().handle(_set(_PLANTED, ref="github.token"))
    assert stored.events == ()
    assert stored.route_ref is None
    deleted = _handler().handle(_delete("github.token"))
    assert deleted.events == ()


@pytest.mark.parametrize(
    ("first", "request_", "reason"),
    [
        (True, _set(_SECOND), "already has a stored value"),
        (False, _delete(), "holds no value"),
        (False, _set(""), "no value was supplied"),
    ],
)
def test_h5_a_refusal_names_no_value_and_emits_nothing(
    store_path: Path,
    first: bool,
    request_: ModelLocalSecretRequest,
    reason: str,
) -> None:
    if first:
        _handler().handle(_set(_PLANTED))
    with pytest.raises(LocalSecretStoreRefusedError, match=reason) as refused:
        _handler().handle(request_)
    for text in (str(refused.value), repr(refused.value)):
        assert _PLANTED not in text
        assert _SECOND not in text


def test_r1_register_then_revoke_ends_revoked_with_its_fingerprint(
    store_path: Path, tmp_path: Path
) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "projection.sqlite")
    events = (
        _handler().handle(_set(_PLANTED)).events + _handler().handle(_delete()).events
    )
    _fold(events, db)

    [row] = _rows(db)
    assert row["revoked_at"] is not None
    assert row["fingerprint"] == hashlib.sha256(_PLANTED.encode()).hexdigest()[:8]
    assert row["provider"] == "openrouter"
    assert row["name"] == _REF


def test_r2_the_other_delivery_order_ends_the_same(
    store_path: Path, tmp_path: Path
) -> None:
    events = (
        _handler().handle(_set(_PLANTED)).events + _handler().handle(_delete()).events
    )
    in_order = SqliteDatabaseAdapter(tmp_path / "in-order.sqlite")
    reversed_ = SqliteDatabaseAdapter(tmp_path / "reversed.sqlite")
    _fold(events, in_order)
    _fold(tuple(reversed(events)), reversed_)

    def shape(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            {**row, "created_at": None, "revoked_at": row["revoked_at"] is not None}
            for row in rows
        ]

    assert shape(_rows(reversed_)) == shape(_rows(in_order))


def test_r3_a_duplicate_delivery_converges(store_path: Path, tmp_path: Path) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "projection.sqlite")
    events = _handler().handle(_set(_PLANTED)).events
    _fold(events, db)
    once = _rows(db)
    _fold(events, db)
    assert _rows(db) == once


def test_r4_delete_then_set_writes_a_second_live_row(
    store_path: Path, tmp_path: Path
) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "projection.sqlite")
    first = _handler().handle(_set(_PLANTED))
    events = first.events + _handler().handle(_delete()).events
    second = _handler().handle(_set(_SECOND))
    _fold(events + second.events, db)

    rows = {str(row["api_key_ref"]): row for row in _rows(db)}
    assert set(rows) == {first.route_ref, second.route_ref}
    assert rows[str(first.route_ref)]["revoked_at"] is not None
    assert rows[str(second.route_ref)]["revoked_at"] is None
    assert rows[str(second.route_ref)]["name"] == _REF


def test_r5_a_re_set_leaves_one_live_key_for_the_provider(
    store_path: Path, tmp_path: Path
) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "projection.sqlite")
    events = _handler().handle(_set(_PLANTED)).events
    events += _handler().handle(_set(_SECOND, force=True)).events
    _fold(events, db)

    live = [row for row in _rows(db) if row["revoked_at"] is None]
    assert len(live) == 1
    assert live[0]["fingerprint"] == hashlib.sha256(_SECOND.encode()).hexdigest()[:8]
