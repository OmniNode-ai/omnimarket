# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: replay the recorded pairs of GET /v1/tenants/me/delegations against the mounted app.

AC3 (fixture replay) is pending on T18b, OMN-22158; the corpus test runs once the
corpus lands. The machinery tests below always run, so the replay cannot pass
vacuously: they prove it accepts a matching pair and fails on an unmasked change.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.nodes.tenant_read_replay import SKIP_REASON, corpus_pairs, replay

pytestmark = pytest.mark.unit
NODE = "node_tenant_delegations_read_effect"
IDENTITIES = {("GET", "/v1/tenants/me/delegations")}
MASK = "<MASK:private_text>"


def _pair(status: int = 200) -> dict[str, Any]:
    run = {
        "run_id": MASK,
        "task_type": MASK,
        "model": MASK,
        "status": MASK,
        "cost_usd": 0.01,
        "saved_usd": 0.42,
        "occurred_at": MASK,
    }
    return {
        "method": "GET",
        "path": "/v1/tenants/me/delegations",
        "response": {
            "status": status,
            "headers": {},
            "body": {
                "generated_at": MASK,
                "count": 2,
                "data_state": MASK,
                "runs": [dict(run), dict(run)],
            },
        },
        "masks": {
            **{
                f"/response/body/runs/{i}/{k}": "private_text"
                for i in (0, 1)
                for k in ("run_id", "task_type", "model", "status", "occurred_at")
            },
            "/response/body/generated_at": "private_text",
            "/response/body/data_state": "private_text",
        },
    }


async def test_replay_accepts_a_matching_pair() -> None:
    assert await replay(NODE, _pair()) == []


async def test_replay_fails_on_a_difference_outside_the_named_masks() -> None:
    changed = copy.deepcopy(_pair())
    changed["response"]["body"]["count"] = 99
    assert await replay(NODE, changed) != []
    unmasked = copy.deepcopy(_pair())
    del unmasked["masks"]["/response/body/runs/0/model"]
    assert await replay(NODE, unmasked) != []


async def test_replay_reproduces_a_recorded_refusal() -> None:
    refusal = {
        "method": "GET",
        "path": "/v1/tenants/me/delegations",
        "response": {"status": 401, "headers": {}, "body": {"detail": MASK}},
        "masks": {"/response/body/detail": "private_text"},
    }
    assert await replay(NODE, refusal) == []


async def test_t18b_corpus_replays_with_no_unmasked_difference() -> None:
    pairs = corpus_pairs(IDENTITIES)
    if not pairs:
        pytest.skip(SKIP_REASON)
    failures = {
        index: differences
        for index, pair in enumerate(pairs)
        if (differences := await replay(NODE, pair))
    }
    assert not failures, failures
