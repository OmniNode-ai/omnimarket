# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host-bound tenant key is never the shared delegation route's key (OMN-22822).

The fixture is the dev lane's catalog row for the h200 review key
(``review-h200-8``, ``bound_host=h200``), registered 2026-11-04T02:46Z and
revoked 2026-11-06T15:14Z. Its value lives only in h200's local secret store.
Between 2026-11-04 and 2026-11-05 the pool selector chose it for 1,262 dev-lane
delegations of the same tenant, the effect on the dev lane resolved it from
Infisical, and every call failed with "Failed to resolve secret from
Infisical (SecretResolutionError)" -- the failures the dead-letter replays later
carried. The test reads the pool at 2026-11-05T09:45Z, when the key was live.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_delegation_routing_reducer.tenant_key_selection import (
    TENANT_CREDENTIALS_TABLE,
    _select_pool_secret_ref,
)

pytestmark = pytest.mark.unit

_ROW_FILE = (
    Path(__file__).parent
    / "fixtures"
    / "omn22822_replayed_responses"
    / "review_h200_credential_row.json"
)
_ROUTED_AT = datetime(2026, 11, 5, 9, 45, tzinfo=UTC)
_CORRELATION = "01a3290e-af41-4873-b313-fbc3c5d443b4"


class _Catalog:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def query(
        self, table: str, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if table != TENANT_CREDENTIALS_TABLE:
            return []
        return [
            dict(r)
            for r in self._rows
            if all(r.get(k) == v for k, v in (filters or {}).items())
        ]


def _recorded_row() -> dict[str, Any]:
    row: dict[str, Any] = json.loads(_ROW_FILE.read_text())["row"]
    return row


def _row_when_routed() -> dict[str, Any]:
    """The catalog row as the selector read it on 2026-11-05: not yet revoked."""
    return dict(_recorded_row(), revoked_at=None)


def _pool(rows: list[dict[str, Any]], overlay_ref: str | None) -> str | None:
    return _select_pool_secret_ref(
        _Catalog(rows),
        tenant_id=rows[0]["tenant_id"],
        provider="anthropic",
        overlay_secret_ref=overlay_ref,
        selector_key=_CORRELATION,
        now=_ROUTED_AT,
    )


@pytest.mark.unit
def test_recorded_review_key_was_live_and_host_bound_when_routed() -> None:
    row = _recorded_row()
    assert row["bound_host"] == "h200"
    assert datetime.fromisoformat(row["created_at"]) < _ROUTED_AT
    assert datetime.fromisoformat(row["revoked_at"]) > _ROUTED_AT


@pytest.mark.unit
def test_host_bound_review_key_is_not_the_route_key() -> None:
    row = _row_when_routed()
    assert _pool([row], row["api_key_ref"]) is None


@pytest.mark.unit
def test_an_unbound_key_of_the_same_tenant_is_still_chosen() -> None:
    row = _row_when_routed()
    unbound = dict(
        row,
        api_key_ref=row["api_key_ref"][:-32] + "0" * 32,
        bound_host=None,
        name="pool-key",
    )
    assert _pool([row, unbound], row["api_key_ref"]) == unbound["api_key_ref"]
