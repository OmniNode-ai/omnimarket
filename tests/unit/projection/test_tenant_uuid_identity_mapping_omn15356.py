# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15356: every landed tenant value maps totally to a canonical UUID.

Scope of this ticket (per its own body): "Standardize canonical UUID identity
only for relations classified TENANT. Produce an explicit legacy slug/
text/sentinel-to-UUID mapping, reject unmapped or ambiguous values ...". This
file proves the mapping function that migration 0003 (capability_scores) and
every subsequent classified-TENANT migration call to perform the TEXT->UUID
column conversion.

The mapping is deliberately total-over-a-closed-domain, not partial: the only
legacy value any landed migration has ever written is
``HOUSE_TENANT_SLUG`` ("omninode", see ``tenant_isolation.py``). Anything else
seen in a real column is either an operator data-entry error or a tenant this
codebase does not yet know about -- either way, silently inventing a UUID for
it (e.g. via ``uuid5`` on the unknown string) would let an unreviewed value
become a new tenant identity with no registry entry and no RLS policy
covering it. The predicate is "no sentinel survives", so unmapped input must
raise, never fall back to a default.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.projection.tenant_isolation import (
    HOUSE_TENANT_SLUG,
    HOUSE_TENANT_UUID,
    UnmappedTenantIdentityError,
    resolve_tenant_uuid,
)

pytestmark = pytest.mark.unit


def test_the_house_tenant_slug_resolves_to_its_pinned_canonical_uuid() -> None:
    assert resolve_tenant_uuid(HOUSE_TENANT_SLUG) == HOUSE_TENANT_UUID


def test_resolution_is_total_for_every_value_a_landed_migration_ever_wrote() -> None:
    """Every DEFAULT/backfill value any landed migration wrote is mappable.

    Regresses if a new relation lands with a different literal DEFAULT and
    nobody teaches this resolver about it -- the migration that calls it would
    then fail closed against its OWN seed data, which is the correct failure
    mode but should be caught here first, in a fast unit test, not in the
    Docker proof.
    """
    assert resolve_tenant_uuid("omninode") == UUID(
        "820272f9-4aaf-5add-a2df-0af942852ab2"
    )


@pytest.mark.parametrize(
    "unmapped_value",
    [
        "default",  # the OMN-15655 orphan literal -- must stay unmapped, not silently absorbed
        "acme-legacy",
        "OMNINODE",  # case must not be silently folded -- ambiguity is refused, not guessed
        " omninode",  # leading whitespace is a distinct, unreviewed value
        "omninode ",  # trailing whitespace likewise
        "",
        "820272f9-4aaf-5add-a2df-0af942852ab2",  # the UUID string itself is not a valid slug input
    ],
)
def test_unmapped_or_ambiguous_values_are_refused_not_defaulted(
    unmapped_value: str,
) -> None:
    with pytest.raises(UnmappedTenantIdentityError):
        resolve_tenant_uuid(unmapped_value)


def test_none_is_refused() -> None:
    with pytest.raises(UnmappedTenantIdentityError):
        resolve_tenant_uuid(None)


def test_no_sentinel_uuid_is_ever_returned_on_the_refusal_path() -> None:
    """A defensive-default UUID (nil, or a re-derivation of the bad input)
    would recreate the exact orphan-tenant defect OMN-15655 fixed for
    'default' -- prove refusal is an exception, never a return value."""
    with pytest.raises(UnmappedTenantIdentityError) as exc_info:
        resolve_tenant_uuid("some-unknown-tenant")
    assert "some-unknown-tenant" in str(exc_info.value)
