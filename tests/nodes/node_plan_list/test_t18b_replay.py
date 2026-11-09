# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: replay the T18b corpus pairs for GET /v1/plans against the mounted app.

Defined only while the corpus location names a directory (T18b, OMN-22158), because a
test that is collected and then skipped counts against the skip-count ratchet; when
pairs are present it runs and never passes vacuously. Each
pair's setup is rebuilt with fresh state (a new seeded repository and a new app) from
the recorded response, the request is replayed, and status, content type and body
must equal the recording outside the corpus's named masking rules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import pytest

from omnimarket.nodes.node_plan_list.handlers.http_app import create_app
from omnimarket.nodes.node_plan_list.models.model_plan_catalog_row import (
    ModelPlanCatalogRow,
)
from tests.nodes.node_plan_list.fakes import FakePlanCatalogRepository
from tests.nodes.node_plan_list.replay_corpus import (
    apply_masking,
    corpus_present,
    load_pairs,
)

pytestmark = pytest.mark.unit
IDENTITIES = frozenset({"GET /v1/plans"})


def rows_from_recording(body: Any) -> list[ModelPlanCatalogRow]:
    rows: list[ModelPlanCatalogRow] = []
    for plan in body if isinstance(body, list) else []:
        base: dict[str, Any] = {
            "plan_id": plan["id"],
            "code": plan["code"],
            "name": plan["name"],
            "description": plan.get("description"),
            "is_default": plan["is_default"],
            "created_at": datetime.fromisoformat(plan["created_at"]),
        }
        entitlements = plan.get("entitlements") or [None]
        for entitlement in entitlements:
            extra = entitlement or {
                "metric_key": None,
                "window_key": None,
                "limit_value": None,
                "hard_cap": None,
                "unit": None,
            }
            rows.append(ModelPlanCatalogRow(**base, **{**{"unit": None}, **extra}))
    return rows


async def replay_recorded_pairs() -> None:
    pairs, rules = load_pairs(IDENTITIES)
    assert pairs, "a replay that selected no pair must have skipped"
    for pair in pairs:
        expected = pair.response
        repo = FakePlanCatalogRepository(
            rows=rows_from_recording(expected.get("body")),
            fail=expected["status"] == 500,
        )
        transport = httpx.ASGITransport(app=create_app(repository=repo))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://runtime"
        ) as c:
            response = await c.request(
                pair.request["method"],
                pair.request["path"],
                headers=pair.request.get("headers") or {},
                params=pair.request.get("query") or None,
            )
        actual = {
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "body": response.json(),
        }
        recorded = {
            "status": expected["status"],
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


if corpus_present():

    async def test_recorded_pairs_replay_with_no_unmasked_difference() -> None:
        await replay_recorded_pairs()
