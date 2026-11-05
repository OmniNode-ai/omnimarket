# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fakes bound at the DI ports for the in-memory profile."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import httpx

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers.http_app import (
    create_app,
)

ROUTE = "/v1/projections/hook-events/by-correlation"
CID = "01J8ZC9K7Q0000000000000001"
SLUG_A = "tenant-alpha"
SLUG_B = "tenant-bravo"
KEY_A = "key-alpha"
KEY_B = "key-bravo"


def tenant(slug: str) -> ModelHttpTenantIdentity:
    return ModelHttpTenantIdentity(
        tenant_id=UUID(
            "00000000-0000-0000-0000-00000000000" + ("1" if slug == SLUG_A else "2")
        ),
        tenant_slug=slug,
        principal_id=f"principal-{slug}",
    )


class FakeIdentity:
    """Test fake: resolves only the seeded keys; anything else is no tenant."""

    def __init__(self, keys: Mapping[str, str] | None = None) -> None:
        self.keys = dict(keys if keys is not None else {KEY_A: SLUG_A, KEY_B: SLUG_B})
        self.calls = 0

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        self.calls += 1
        slug = self.keys.get(headers.get("x-api-key", ""))
        return tenant(slug) if slug else None


def row(slug: str = SLUG_A, cid: str = CID, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tenant_id": slug,
        "event_sha": "a" * 64,
        "event_type": "tool-executed",
        "occurred_at": dt.datetime(2026, 8, 30, 15, 6, 24, tzinfo=dt.UTC),
        "captured_at": dt.datetime(2026, 8, 30, 15, 6, 25, tzinfo=dt.UTC),
        "event_id": f"evt-{slug}",
        "correlation_id": cid,
        "run_id": "run-1",
        "source": "omniclaude-hook",
        "payload": {"tool": "Bash", "secret_looking_field": "do-not-leak"},
    }
    base.update(over)
    return base


class FakeReader:
    """In-memory repository: filters strictly by the tenant it is handed."""

    def __init__(
        self,
        rows: Sequence[Mapping[str, Any]] = (),
        raises: Exception | None = None,
    ) -> None:
        self.rows = list(rows)
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def read(
        self, *, tenant_slug: str, correlation_id: str, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append(
            {
                "tenant_slug": tenant_slug,
                "correlation_id": correlation_id,
                "limit": limit,
            }
        )
        if self.raises is not None:
            raise self.raises
        return [
            r
            for r in self.rows
            if r["tenant_id"] == tenant_slug and r["correlation_id"] == correlation_id
        ][:limit]


def client(
    reader: FakeReader, identity: FakeIdentity | None = None
) -> httpx.AsyncClient:
    app = create_app(reader=reader, identity=identity or FakeIdentity())
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture.invalid"
    )
