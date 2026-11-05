# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The replay engine, proven on a synthetic corpus built in tmp_path.

The synthetic pairs below are written here, not recorded; they prove the engine
selects this route's identity, rebuilds state, applies named masks and fails on
an unmasked difference. They are not evidence for AC3 (pending on T18b,
OMN-22158).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.nodes.node_hook_events_by_correlation_read_effect.replay_support import (
    IDENTITY,
    SKIP_REASON,
    replay_all,
    select_pairs,
)

pytestmark = pytest.mark.unit
PATH = IDENTITY[1]
FOUND = {
    "correlation_id": "c1",
    "projection": "hook_events",
    "data_state": "found",
    "tenant_scope": "t-a",
    "count": 1,
    "rows": [
        {
            "correlation_id": "c1",
            "event_id": "e1",
            "run_id": "r1",
            "event_type": "x.v1",
            "source": "s",
            "tenant_id": "t-a",
            "occurred_at": "2026-08-30T15:06:24Z",
            "captured_at": "2026-08-30T15:06:25Z",
            "payload": None,
        }
    ],
    "generated_at": "<masked:generated_at>",
}


def corpus(
    tmp_path, pairs, rules=({"name": "generated_at", "path": "$.generated_at"},)
):
    entries = []
    for i, (identity, request, response) in enumerate(pairs):
        rq, rs = json.dumps(request).encode(), json.dumps(response).encode()
        (tmp_path / f"{i}.req.json").write_bytes(rq)
        (tmp_path / f"{i}.res.json").write_bytes(rs)
        entries.append(
            {
                "identity": identity,
                "request": f"{i}.req.json",
                "response": f"{i}.res.json",
                "sha256": {
                    "request": hashlib.sha256(rq).hexdigest(),
                    "response": hashlib.sha256(rs).hexdigest(),
                },
                "cleanup": "none",
            }
        )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"masking_rules": list(rules), "pairs": entries})
    )
    return tmp_path


REQ = {
    "method": "GET",
    "path": PATH,
    "query": {"correlation_id": "c1"},
    "headers": {"x-api-key": "<masked>"},
}


async def test_matching_pair_replays_clean(tmp_path):
    root = corpus(tmp_path, [(f"GET {PATH}", REQ, {"status": 200, "body": FOUND})])
    count, problems = await replay_all(root)
    assert count == 1
    assert problems == []


async def test_unmasked_difference_fails(tmp_path):
    changed = {**FOUND, "count": 7}
    root = corpus(tmp_path, [(f"GET {PATH}", REQ, {"status": 200, "body": changed})])
    count, problems = await replay_all(root)
    assert count == 1
    assert problems
    assert "differs" in problems[0]


async def test_an_unmasked_timestamp_is_a_difference(tmp_path):
    root = corpus(
        tmp_path,
        [
            (
                f"GET {PATH}",
                REQ,
                {
                    "status": 200,
                    "body": {**FOUND, "generated_at": "2026-01-01T00:00:00Z"},
                },
            )
        ],
        rules=(),
    )
    _, problems = await replay_all(root)
    assert problems


async def test_a_sha256_mismatch_fails(tmp_path):
    root = corpus(tmp_path, [(f"GET {PATH}", REQ, {"status": 200, "body": FOUND})])
    (tmp_path / "0.res.json").write_text("{}")
    with pytest.raises(AssertionError, match="sha256 mismatch"):
        await replay_all(root)


async def test_refusal_pairs_replay(tmp_path):
    root = corpus(
        tmp_path,
        [
            (
                f"GET {PATH}",
                {**REQ, "headers": {}},
                {
                    "status": 401,
                    "body": {
                        "detail": "Unauthorized",
                        "code": "auth.credential.absent",
                    },
                },
            ),
            (
                f"GET {PATH}",
                REQ,
                {
                    "status": 503,
                    "body": {"detail": "Ledger projection database is unavailable"},
                },
            ),
            (
                f"GET {PATH}",
                REQ,
                {
                    "status": 200,
                    "body": {
                        **FOUND,
                        "data_state": "projection_absent",
                        "count": 0,
                        "rows": [],
                    },
                },
            ),
        ],
    )
    count, problems = await replay_all(root)
    assert count == 3
    assert problems == []


def test_other_identities_are_not_selected(tmp_path):
    root = corpus(tmp_path, [("GET /v1/other/route", REQ, {"status": 200, "body": {}})])
    pairs, _ = select_pairs(root)
    assert pairs == []
    assert "T18b" in SKIP_REASON
    assert "OMN-22158" in SKIP_REASON
