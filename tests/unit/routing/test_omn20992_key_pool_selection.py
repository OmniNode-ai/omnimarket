# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20992: delegation chooses among a tenant's live keys for the routed provider.

Failure modes, each with a test below:
  S1  a tenant with several live keys always routes to the one registered last
      (the overlay row's secret_ref), so the pool carries one key's load;
  S2  the choice is not stable for one request, so a retry of the same
      correlation id lands on a different key;
  S3  a revoked or expired key is selected;
  S4  with no live key left the overlay keeps a stale secret_ref, or falls back
      to a house key, instead of failing closed with secret_ref unset;
  S5  a key for another provider, or another tenant's key, is selected;
  S6  a tenant with no catalogued keys (a house overlay row) changes at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.routing.tenant_overlay_resolver import (
    BYOK_ALL_TASK_TYPES,
    TENANT_CREDENTIALS_TABLE,
    TENANT_OVERLAY_TABLE,
    resolve_tenant_overlay,
)

pytestmark = pytest.mark.unit

_TENANT = "omninode"
_TASK = "code_review"
_NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
_JAN_1 = datetime(2027, 1, 1, tzinfo=UTC)


def _ref(n: int, provider: str = "anthropic", tenant: str = _TENANT) -> str:
    return f"cred_{tenant}_{provider}_{n:032x}"


class _Reader:
    """Equality-filter reader over two in-memory tables."""

    def __init__(
        self,
        overlay: list[dict[str, object]],
        credentials: list[dict[str, object]],
    ) -> None:
        self._tables = {
            TENANT_OVERLAY_TABLE: overlay,
            TENANT_CREDENTIALS_TABLE: credentials,
        }
        self.queried: list[str] = []

    def query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        self.queried.append(table)
        rows = self._tables.get(table, [])
        return [
            row
            for row in rows
            if all(row.get(k) == v for k, v in (filters or {}).items())
        ]


def _overlay(secret_ref: str | None, provider: str = "anthropic") -> dict[str, object]:
    return {
        "tenant_id": _TENANT,
        "task_type": BYOK_ALL_TASK_TYPES,
        "backend_id": f"byok-{provider}",
        "provider": provider,
        "endpoint_url": "https://example.invalid/v1",
        "model_name": "claude-model",
        "secret_ref": secret_ref,
        "timeout_ms": None,
        "max_tokens": None,
    }


def _cred(
    ref: str,
    *,
    provider: str = "anthropic",
    tenant: str = _TENANT,
    revoked_at: object = None,
    expires_at: object = _JAN_1,
) -> dict[str, object]:
    return {
        "api_key_ref": ref,
        "tenant_id": tenant,
        "provider": provider,
        "name": ref[-4:],
        "revoked_at": revoked_at,
        "expires_at": expires_at,
    }


def _pool(*creds: dict[str, object], overlay_ref: str | None = None) -> _Reader:
    return _Reader(
        [_overlay(overlay_ref if overlay_ref is not None else _ref(4))], list(creds)
    )


def _selected(reader: _Reader, key: str | None) -> str | None:
    overlay = resolve_tenant_overlay(
        reader, tenant_id=_TENANT, task_type=_TASK, selector_key=key, now=_NOW
    )
    assert overlay is not None
    return overlay.secret_ref


def test_key_pool_selection_spreads_requests_across_every_live_key() -> None:
    """S1: over many correlation ids, every live key is chosen."""
    reader = _pool(*(_cred(_ref(n)) for n in range(1, 5)))
    chosen = {_selected(reader, f"corr-{i}") for i in range(200)}
    assert chosen == {_ref(n) for n in range(1, 5)}


def test_key_pool_selection_is_stable_for_one_correlation_id() -> None:
    """S2: the same request id lands on the same key every time."""
    reader = _pool(*(_cred(_ref(n)) for n in range(1, 5)))
    assert len({_selected(reader, "corr-fixed") for _ in range(20)}) == 1


def test_key_pool_selection_ignores_row_order() -> None:
    """S2: the choice depends on the pool, not on the order a read returns it."""
    creds = [_cred(_ref(n)) for n in range(1, 5)]
    forward = _pool(*creds)
    backward = _pool(*reversed(creds))
    for i in range(50):
        assert _selected(forward, f"c{i}") == _selected(backward, f"c{i}")


def test_key_pool_selection_ignores_other_providers_and_tenants() -> None:
    """S5: only this tenant's keys for the overlay's provider are candidates."""
    reader = _pool(
        _cred(_ref(1)),
        _cred(_ref(2, provider="openrouter"), provider="openrouter"),
        _cred(_ref(3, tenant="someone-else"), tenant="someone-else"),
    )
    assert {_selected(reader, f"c{i}") for i in range(50)} == {_ref(1)}


def test_key_pool_skips_expired_and_revoked_keys() -> None:
    """S3: a lapsed or revoked key is never chosen."""
    reader = _pool(
        _cred(_ref(1)),
        _cred(_ref(2), expires_at=_NOW - timedelta(seconds=1)),
        _cred(_ref(3), revoked_at=_NOW - timedelta(days=1)),
        _cred(_ref(4), expires_at=_NOW),
    )
    assert {_selected(reader, f"c{i}") for i in range(100)} == {_ref(1)}


def test_key_pool_skips_expired_keys_read_as_iso_text() -> None:
    """S3: the local store returns timestamps as ISO text, not datetimes."""
    reader = _pool(
        _cred(_ref(1), expires_at="2027-01-01T00:00:00+00:00"),
        _cred(_ref(2), expires_at="2026-01-01T00:00:00+00:00"),
    )
    assert {_selected(reader, f"c{i}") for i in range(50)} == {_ref(1)}


def test_key_pool_skips_expired_and_fails_closed_when_none_is_live() -> None:
    """S4: every key lapsed means no key, never the stale overlay ref."""
    reader = _pool(
        _cred(_ref(1), expires_at=_NOW - timedelta(days=1)),
        _cred(_ref(4), revoked_at=_NOW),
        overlay_ref=_ref(4),
    )
    overlay = resolve_tenant_overlay(
        reader, tenant_id=_TENANT, task_type=_TASK, selector_key="c", now=_NOW
    )
    assert overlay is not None, "the row is kept so the tenant never falls back"
    assert overlay.secret_ref is None
    assert overlay.backend_id == "byok-anthropic"


def test_key_pool_keeps_an_undated_key_live() -> None:
    """A key registered before expiry existed has no date and stays usable."""
    reader = _pool(_cred(_ref(1), expires_at=None))
    assert _selected(reader, "c") == _ref(1)


@pytest.mark.parametrize("selector_key", [None, "corr-first", "corr-retry"])
@pytest.mark.parametrize("stored_task_type", [BYOK_ALL_TASK_TYPES, _TASK])
def test_key_pool_single_undated_key_preserves_the_complete_overlay(
    selector_key: str | None, stored_task_type: str
) -> None:
    """AC3: the pool path preserves every field of a single-key binding."""
    row = _overlay(_ref(1))
    row.update(task_type=stored_task_type, timeout_ms=12000, max_tokens=4096)
    reader = _Reader([row], [_cred(_ref(1), expires_at=None)])

    overlay = resolve_tenant_overlay(
        reader,
        tenant_id=_TENANT,
        task_type=_TASK,
        selector_key=selector_key,
        now=_NOW,
    )

    assert overlay is not None
    assert overlay.model_dump() == {**row, "task_type": _TASK}
    assert TENANT_CREDENTIALS_TABLE in reader.queried


def test_key_pool_without_a_selector_key_keeps_the_overlay_ref_when_live() -> None:
    reader = _pool(_cred(_ref(1)), _cred(_ref(4)), overlay_ref=_ref(4))
    assert _selected(reader, None) == _ref(4)


def test_key_pool_leaves_an_uncatalogued_overlay_unchanged() -> None:
    """S6: a house overlay row has no catalogued keys; nothing about it changes."""
    reader = _Reader([_overlay("llm.house.api_key", provider="house")], [])
    assert _selected(reader, "c") == "llm.house.api_key"


def test_key_pool_catalog_read_failure_keeps_the_overlay_as_read() -> None:
    """An unreadable catalog leaves today's single-key behaviour, logged."""

    class _Broken(_Reader):
        def query(
            self, table: str, filters: dict[str, object] | None = None
        ) -> list[dict[str, object]]:
            if table == TENANT_CREDENTIALS_TABLE:
                raise RuntimeError("permission denied for table")
            return super().query(table, filters)

    reader = _Broken([_overlay(_ref(4))], [])
    assert _selected(reader, "c") == _ref(4)


@pytest.mark.parametrize(
    "credential",
    [
        {},
        {"api_key_ref": None},
        {"api_key_ref": ""},
        {"api_key_ref": " "},
        {"api_key_ref": 123},
    ],
)
def test_key_pool_malformed_catalog_keeps_the_overlay_as_read(
    credential: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable credential identity is not evidence of an exhausted pool."""
    reader = _Reader(
        [_overlay(_ref(4))],
        [{"tenant_id": _TENANT, "provider": "anthropic", **credential}],
    )
    assert _selected(reader, "c") == _ref(4)
    assert "tenant key pool read failed" in caplog.text
