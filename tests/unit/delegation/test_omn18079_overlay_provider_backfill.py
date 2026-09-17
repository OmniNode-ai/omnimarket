# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18079: the overlay provider backfill states only declared provenance.

``0004_add_delegation_routing_tenant_overlay_provider.sql`` added ``provider``
as a NULLABLE column and backfilled nothing, while
``handler_delegation_routing._decision_from_tenant_overlay`` began REFUSING any
overlay row whose provider is blank.  Every row minted before that migration
therefore stopped routing at once -- observed live on ``onex-dev`` at
2026-09-16T00:40:04Z, where all 33 ``delegation_routing_tenant_overlay`` rows
carried ``provider IS NULL`` and the staging business proof (OMN-15256)
terminalised ``failed`` with ``ProtocolConfigurationError
[ONEX_CORE_041_INVALID_CONFIGURATION] Tenant routing overlay 'byok-glm' has no
declared provider provenance``.

``0005`` repairs that data.  The provenance it writes is not inferred: the one
writer of this table (``node_projection_tenant_credentials``) mints
``backend_id`` and ``provider`` from the SAME
``configs/byok_provider_backends.v1.yaml`` entry, so ``backend_id -> provider``
inverts a declared binding rather than guessing one.  These tests are what stop
that from decaying into a guess.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.routing.byok_provider_backends import load_byok_provider_catalog

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegation_routing_reducer"
    / "migrations"
    / "0005_backfill_delegation_routing_tenant_overlay_provider.sql"
)

#: ``('byok-glm', 'glm')`` pairs as written in the migration's VALUES list.
_PAIR_RE = re.compile(r"\(\s*'([a-z0-9_-]+)'\s*,\s*'([a-z0-9_-]+)'\s*\)")


def _migration_sql() -> str:
    assert MIGRATION_PATH.is_file(), f"missing backfill migration: {MIGRATION_PATH}"
    return MIGRATION_PATH.read_text()


def _executable_sql() -> str:
    """The migration with every comment line removed.

    Prose in this file names the very shapes the tests below refuse -- it has
    to, to explain why they are refused -- so a substring assertion run over
    the raw text would read the explanation as the thing explained.
    """
    body = _migration_sql()
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("--")
    )


def _declared_pairs() -> dict[str, str]:
    """The (backend_id -> provider) pairs the migration actually writes."""
    return dict(_PAIR_RE.findall(_executable_sql()))


@pytest.mark.unit
def test_backfill_pairs_are_exactly_the_declared_catalogue_bindings() -> None:
    """Every pair the migration writes is a binding the catalogue declares.

    Falsifier: change one provider slug in the migration, or bind a backend_id
    the catalogue does not name, and this fails.
    """
    catalogue = {
        entry.backend_id: entry.provider
        for entry in load_byok_provider_catalog().values()
    }
    assert _declared_pairs() == catalogue


@pytest.mark.unit
def test_backfill_never_overwrites_an_existing_provider() -> None:
    """The UPDATE is guarded by ``provider IS NULL``.

    A row the writer has already stamped carries a FACT about the route that
    answered; a re-run of this migration must not restate it from a mapping.
    """
    sql = _executable_sql().lower()
    assert "update delegation_routing_tenant_overlay" in sql
    assert "provider is null" in sql


@pytest.mark.unit
def test_backfill_leaves_an_uncatalogued_backend_id_null() -> None:
    """No ELSE branch, no COALESCE default, no blanket UPDATE.

    A row naming a backend the catalogue does not declare has no knowable
    provenance, and the routing resolver must keep refusing it.  Writing a
    placeholder there would convert a fail-closed refusal into a wrong route.
    """
    sql = _executable_sql().lower()
    assert "coalesce" not in sql
    assert "'unknown'" not in sql
    # The join is what bounds the write to catalogued backend_ids.
    assert "o.backend_id = declared.backend_id" in sql
