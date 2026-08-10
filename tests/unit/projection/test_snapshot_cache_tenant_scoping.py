# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15800 corrective round -- Defect B: proves the cross-tenant exposure
the verifier's live 3-tenant probe found, and pins it as an explicit, known,
tracked gap rather than a silent one.

``SnapshotCache.get_rows`` has no tenant argument and no tenant filter --
every row cached for a topic is returned to every caller regardless of the
``tenant_id`` header carried on the delta that produced it. This is not
patched here: real scoping needs a per-event tenant identity this reducer
can read, and OMN-15800's live envelope (``omnibase_core.ModelEventEnvelope``)
carries no ``tenant_id`` field anywhere in its schema (verified by direct
source read, not inference) -- see
``tests/unit/projection/test_house_tenant_default_ratchet.py`` for the
independent, pre-existing proof that the broader tenant-authority path
(``bind_projection_tenant_authority`` / ``verify_signed_projection_tenant_authority``)
has zero non-test call sites in shipped ``omnibase_infra`` today.

The fix taken instead (test_projection_discovery.py::TestOmn15800TenantScopingFallback)
is contract-level: ``savings.v1`` -- the one exposure with real multi-tenant
data (18 rows / 3 tenants) -- is kept OFF ``bus_backed``, so this cache-level
defect is never reached by a real HTTP caller. This test exists so that
re-flipping ``savings.v1`` (or any other multi-tenant family) back to
``bus_backed: true`` without ALSO fixing this method is a conscious,
test-visible decision, not a silent regression.
"""

from __future__ import annotations

import json

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache

_TOPIC = "onex.snapshot.projection.test-multi-tenant.v1"


def _make_cache() -> SnapshotCache:
    exposure = ProjectionTableConfig(
        topic=_TOPIC,
        table="test_table",
        columns=("id", "tenant_id", "amount"),
        bus_backed=True,
        key_columns=("id",),
        limit=100,
    )
    return SnapshotCache({_TOPIC: exposure}, bootstrap_servers="unused:9092")


def _delta_bytes(
    *, row_id: str, tenant_id: str, amount: int, ingest_sequence: int
) -> bytes:
    payload = {
        "topic": _TOPIC,
        "key": [row_id],
        "op": "upsert",
        "row": {"id": row_id, "tenant_id": tenant_id, "amount": amount},
        "observed_at": "2026-08-10T00:00:00Z",
        "source_event_id": f"evt-{row_id}",
        "ingest_sequence": ingest_sequence,
        "projection_version": "projection_snapshot.v1",
    }
    return json.dumps(payload).encode("utf-8")


def test_get_rows_returns_every_tenant_to_an_unscoped_caller() -> None:
    """Reproduces the verifier's live 3-tenant probe shape: publish rows for
    3 distinct tenants, then read with NO tenant scoping applied anywhere
    (matching every current caller of get_rows -- api_server.py passes none).
    An unscoped caller receiving other tenants' rows is the proven defect;
    this test documents it stays true at the cache layer today."""
    cache = _make_cache()

    cache.apply_message(
        _TOPIC,
        key=b"row-a",
        value=_delta_bytes(
            row_id="row-a", tenant_id="tenant-alpha", amount=1, ingest_sequence=1
        ),
        headers=[("tenant_id", b"tenant-alpha")],
    )
    cache.apply_message(
        _TOPIC,
        key=b"row-b",
        value=_delta_bytes(
            row_id="row-b", tenant_id="tenant-beta", amount=2, ingest_sequence=1
        ),
        headers=[("tenant_id", b"tenant-beta")],
    )
    cache.apply_message(
        _TOPIC,
        key=b"row-c",
        value=_delta_bytes(
            row_id="row-c", tenant_id="tenant-gamma", amount=3, ingest_sequence=1
        ),
        headers=[("tenant_id", b"tenant-gamma")],
    )

    # No tenant argument exists on get_rows -- this IS "unscoped".
    rows = cache.get_rows(_TOPIC)
    tenants_returned = {row["tenant_id"] for row in rows}

    assert len(rows) == 3
    assert tenants_returned == {"tenant-alpha", "tenant-beta", "tenant-gamma"}, (
        "SnapshotCache.get_rows leaks every tenant's rows to an unscoped "
        "caller (known, tracked gap -- see module docstring). If this "
        "assertion ever fails because get_rows gained real tenant "
        "filtering, delete this test and lift the savings.v1 bus_backed: "
        "false fallback in the same PR (test_projection_discovery.py::"
        "TestOmn15800TenantScopingFallback)."
    )
