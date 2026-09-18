# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18699: a local install mints its own tenant identity, and the shared
house-tenant constant is never a default on the local path.

Every test here drives the real sqlite store against a tmp_path DB file. None
of them touches the operator's own ``~/.omninode/delegation/delegation.sqlite``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omnimarket.local_deployment.tenant_identity import (
    LOCAL_TENANT_IDENTITY_KEY,
    EnumLocalTenantIdentityRefusalReason,
    LocalTenantIdentityError,
    ModelLocalTenantIdentity,
    local_tenant_identity_or_none,
    mint_local_tenant_identity,
    read_local_tenant_identity,
    require_local_tenant_identity,
    reset_local_tenant_identity_cache,
    resolve_local_deployment_tenant_id,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG, HOUSE_TENANT_UUID
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
    resolve_registry_tenant_uuid,
    sync_registry_tenant_uuid,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    reset_local_tenant_identity_cache()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "delegation.sqlite"


def test_no_identity_in_store_refuses_and_never_returns_the_house_uuid(
    db_path: Path,
) -> None:
    """AC1/AC2 falsifier: a run that proceeds with the shared house constant."""
    assert read_local_tenant_identity(db_path=db_path) is None

    with pytest.raises(LocalTenantIdentityError) as excinfo:
        require_local_tenant_identity(db_path=db_path)

    refusal = excinfo.value.refusal
    assert refusal.reason is EnumLocalTenantIdentityRefusalReason.IDENTITY_ABSENT
    assert refusal.retryable is False
    # The refusal must name the command that fixes it, not merely say "missing".
    assert "onex local init" in str(excinfo.value)
    # And it must never name, or quietly become, the house tenant.
    assert str(HOUSE_TENANT_UUID) not in str(excinfo.value)


def test_resolver_refuses_rather_than_defaulting_to_the_house_tenant(
    db_path: Path,
) -> None:
    """The seam the local dispatch port calls: no identity -> refusal, not a slug."""
    with pytest.raises(LocalTenantIdentityError):
        resolve_local_deployment_tenant_id(None, db_path=db_path)


def test_init_mints_once_and_is_idempotent(db_path: Path) -> None:
    """AC1: the identity is minted at install and does not move afterwards."""
    first = mint_local_tenant_identity(db_path=db_path)
    assert isinstance(first, ModelLocalTenantIdentity)
    assert first.newly_minted is True
    assert first.tenant_uuid != HOUSE_TENANT_UUID

    second = mint_local_tenant_identity(db_path=db_path)
    assert second.newly_minted is False
    assert second.tenant_uuid == first.tenant_uuid
    assert second.tenant_slug == first.tenant_slug

    reset_local_tenant_identity_cache()
    assert require_local_tenant_identity(db_path=db_path) == first.tenant_uuid


def test_minted_identity_is_recorded_under_the_declared_key(db_path: Path) -> None:
    minted = mint_local_tenant_identity(db_path=db_path)
    rows = SqliteDatabaseAdapter(db_path).query(
        "local_deployment_identity", {"key": LOCAL_TENANT_IDENTITY_KEY}
    )
    assert len(rows) == 1
    assert rows[0]["value"] == str(minted.tenant_uuid)


def test_minted_identity_is_confirmable_through_the_shared_registry_resolver(
    db_path: Path,
) -> None:
    """The minted UUID resolves through the SAME confirmation the cloud path uses.

    ``resolve_registry_tenant_uuid`` refuses a UUID the mirror holds no row for.
    A local install that minted an identity but recorded no mirror row would
    therefore produce an unwritable identity -- the OMN-16831 failure shape,
    reproduced locally.
    """
    minted = mint_local_tenant_identity(db_path=db_path)
    db = SqliteDatabaseAdapter(db_path)

    registry_uuid = sync_registry_tenant_uuid(db, str(minted.tenant_uuid))
    assert registry_uuid == minted.tenant_uuid
    assert (
        resolve_registry_tenant_uuid(
            str(minted.tenant_uuid), registry_uuid=registry_uuid
        )
        == minted.tenant_uuid
    )

    mirror = db.query(
        TENANT_REGISTRY_MIRROR_TABLE, {"tenant_uuid": str(minted.tenant_uuid)}
    )
    assert len(mirror) == 1
    assert mirror[0]["tenant_slug"] == minted.tenant_slug


def test_two_installs_mint_distinct_identities(tmp_path: Path) -> None:
    """AC4 falsifier: both installs resolving the same identifier."""
    first = mint_local_tenant_identity(db_path=tmp_path / "a" / "delegation.sqlite")
    reset_local_tenant_identity_cache()
    second = mint_local_tenant_identity(db_path=tmp_path / "b" / "delegation.sqlite")
    assert first.tenant_uuid != second.tenant_uuid


def test_explicit_identity_is_accepted_for_an_existing_install(db_path: Path) -> None:
    """The house install on this Mac keeps the identity its 258 rows already carry."""
    minted = mint_local_tenant_identity(
        db_path=db_path,
        tenant_uuid=HOUSE_TENANT_UUID,
        tenant_slug=HOUSE_TENANT_SLUG,
    )
    assert minted.tenant_uuid == HOUSE_TENANT_UUID
    assert minted.tenant_slug == HOUSE_TENANT_SLUG
    # It is the deployment's OWN explicitly-set identity, not a fallback: the
    # value had to be named on the command line to get here.
    assert require_local_tenant_identity(db_path=db_path) == HOUSE_TENANT_UUID


def test_reminting_a_different_identity_is_refused(db_path: Path) -> None:
    """An install's identity is stable; a second, different one is a refusal."""
    mint_local_tenant_identity(db_path=db_path)
    reset_local_tenant_identity_cache()
    with pytest.raises(LocalTenantIdentityError) as excinfo:
        mint_local_tenant_identity(db_path=db_path, tenant_uuid=uuid4())
    assert (
        excinfo.value.refusal.reason
        is EnumLocalTenantIdentityRefusalReason.IDENTITY_CONFLICT
    )


def test_a_malformed_stored_value_refuses_rather_than_falling_back(
    db_path: Path,
) -> None:
    mint_local_tenant_identity(db_path=db_path)
    SqliteDatabaseAdapter(db_path).upsert(
        "local_deployment_identity",
        "key",
        {
            "key": LOCAL_TENANT_IDENTITY_KEY,
            "value": "not-a-uuid",
            "recorded_at": "2026-09-18T00:00:00+00:00",
        },
    )
    reset_local_tenant_identity_cache()
    with pytest.raises(LocalTenantIdentityError) as excinfo:
        require_local_tenant_identity(db_path=db_path)
    assert (
        excinfo.value.refusal.reason
        is EnumLocalTenantIdentityRefusalReason.IDENTITY_MALFORMED
    )


def test_or_none_reader_is_silent_for_a_deployment_that_never_ran_init(
    db_path: Path,
) -> None:
    """The bus runtime shares this code path and must not start raising."""
    assert local_tenant_identity_or_none(db_path=db_path) is None
    minted = mint_local_tenant_identity(db_path=db_path)
    reset_local_tenant_identity_cache()
    assert local_tenant_identity_or_none(db_path=db_path) == str(minted.tenant_uuid)


def test_a_verified_upstream_tenant_still_wins(db_path: Path) -> None:
    """A gateway-verified identity is never overridden by the local one."""
    mint_local_tenant_identity(db_path=db_path)
    reset_local_tenant_identity_cache()
    verified = str(UUID("91c74442-1233-4c97-b191-911a10346fdf"))
    assert resolve_local_deployment_tenant_id(verified, db_path=db_path) == verified
