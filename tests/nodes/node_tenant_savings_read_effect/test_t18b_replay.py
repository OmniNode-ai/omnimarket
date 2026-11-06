# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: replay the recorded pairs of GET /v1/tenants/me/savings against the mounted app.

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
NODE = "node_tenant_savings_read_effect"
IDENTITIES = {("GET", "/v1/tenants/me/savings")}
MASK = "<MASK:private_text>"


def _pair(status: int = 200) -> dict[str, Any]:
    return {
        "method": "GET",
        "path": "/v1/tenants/me/savings",
        "response": {
            "status": status,
            "headers": {},
            "body": {
                "total_saved_usd": 12.5,
                "total_spend_usd": 3.25,
                "net_saved_usd": 12.5,
                "delegation_runs": 3,
                "currency": MASK,
                "as_of": MASK,
                "data_state": MASK,
            },
        },
        "masks": {
            f"/response/body/{k}": "private_text"
            for k in ("currency", "as_of", "data_state")
        },
    }


async def test_replay_accepts_a_matching_pair() -> None:
    assert await replay(NODE, _pair()) == []


async def test_replay_fails_on_a_difference_outside_the_named_masks() -> None:
    changed = copy.deepcopy(_pair())
    changed["response"]["body"]["net_saved_usd"] = 99.0
    assert await replay(NODE, changed) != []
    unmasked = copy.deepcopy(_pair())
    del unmasked["masks"]["/response/body/currency"]
    assert await replay(NODE, unmasked) != []


async def test_replay_reproduces_a_recorded_refusal() -> None:
    refusal = {
        "method": "GET",
        "path": "/v1/tenants/me/savings",
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
