# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: replay the T18b corpus pairs for GET /v1/usage/privileges against the mounted app.

Skipped only while the corpus location is unset or holds no pair for this identity
(T18b, OMN-22158); when pairs are present it runs and never passes vacuously. Each
pair's setup is rebuilt with fresh state (a new seeded catalog, a new resolver and a
new app) from the recorded response, the request is replayed, and status, content type
and body must equal the recording outside the corpus's named masking rules.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

import httpx
import pytest

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_usage_privileges.handlers.http_app import create_app
from omnimarket.nodes.node_usage_privileges.handlers.identity_binding import (
    bind_tenant_identity_resolver,
)
from omnimarket.nodes.node_usage_privileges.models.model_usage_privilege import (
    ModelUsagePrivilege,
)
from tests.nodes.node_plan_list.replay_corpus import apply_masking, load_pairs
from tests.nodes.node_usage_privileges.fakes import (
    SEEDED_TENANT,
    FakeUsagePrivilegeRepository,
)

pytestmark = pytest.mark.unit
IDENTITIES = frozenset({"GET /v1/usage/privileges"})
CREDENTIAL_HEADERS = ("x-api-key", "authorization")


class RecordedCaller:
    """Resolves the seeded tenant for exactly the credential the recorded request sent."""

    def __init__(self, request_headers: Mapping[str, str], *, accepts: bool) -> None:
        self._accepted = (
            {
                k.lower(): v
                for k, v in request_headers.items()
                if k.lower() in CREDENTIAL_HEADERS
            }
            if accepts
            else None
        )

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        if self._accepted and all(
            headers.get(k) == v for k, v in self._accepted.items()
        ):
            return SEEDED_TENANT
        return None


class _Catalog(FakeUsagePrivilegeRepository):
    def __init__(self, privileges: list[ModelUsagePrivilege], *, fail: bool) -> None:
        super().__init__(fail=fail)
        self._privileges = privileges

    async def fetch_usage_privileges(self) -> list[ModelUsagePrivilege]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("fixture catalog read refusal")
        return self._privileges


@pytest.fixture(autouse=True)
def _unbound() -> Iterator[None]:
    bind_tenant_identity_resolver(None)
    yield
    bind_tenant_identity_resolver(None)


def privileges_from_recording(body: Any) -> list[ModelUsagePrivilege]:
    return [
        ModelUsagePrivilege(
            id=uuid.UUID(item["id"]),
            plan_tier_code=item["plan_tier_code"],
            resource_code=item["resource_code"],
            monthly_quota=item.get("monthly_quota"),
            hard_limit=item["hard_limit"],
            created_at=datetime.fromisoformat(item["created_at"]),
        )
        for item in (body if isinstance(body, list) else [])
    ]


async def test_recorded_pairs_replay_with_no_unmasked_difference() -> None:
    pairs, rules = load_pairs(IDENTITIES)
    assert pairs, "a replay that selected no pair must have skipped"
    for pair in pairs:
        expected = pair.response
        status = expected["status"]
        request_headers = pair.request.get("headers") or {}
        repo = _Catalog(
            privileges_from_recording(expected.get("body")), fail=status == 500
        )
        app = create_app(
            repository=repo,
            identity=None
            if status == 503
            else RecordedCaller(request_headers, accepts=status in (200, 500)),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://runtime"
        ) as c:
            response = await c.request(
                pair.request["method"],
                pair.request["path"],
                headers=request_headers,
                params=pair.request.get("query") or None,
            )
        actual = {
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "body": response.json(),
        }
        recorded = {
            "status": status,
            "content_type": (expected.get("headers") or {}).get("content-type"),
            "body": expected.get("body"),
        }
        if recorded["content_type"] is None:
            actual["content_type"] = None
        assert apply_masking(actual, rules, "response") == apply_masking(
            recorded, rules, "response"
        ), (
            f"{pair.identity}: replay differs from the recording outside the masking rules"
        )
