# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Positive and negative controls for the replay machinery, on a synthetic corpus.

The synthetic pairs below are written to a temporary directory and are not T18b data;
they prove the replay tests run, compare and fail when pairs are present, so a pass or
a skip of the real replay never means an unexercised reader.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.nodes.node_plan_list import test_t18b_replay as plans_replay
from tests.nodes.node_plan_list.replay_corpus import CORPUS_ENV

pytestmark = pytest.mark.unit
PLANS_BODY = [
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "code": "free",
        "name": "Free",
        "description": None,
        "is_default": True,
        "created_at": "2025-01-15T12:00:00Z",
        "entitlements": [
            {
                "metric_key": "events",
                "window_key": "month",
                "limit_value": 5,
                "hard_cap": False,
                "unit": None,
            }
        ],
    }
]


def _write_corpus(root: Path, body: object, *, rules: list[dict[str, str]]) -> None:
    request = json.dumps({"method": "GET", "path": "/v1/plans", "headers": {}}).encode()
    response = json.dumps(
        {"status": 200, "headers": {"content-type": "application/json"}, "body": body}
    ).encode()
    (root / "req.json").write_bytes(request)
    (root / "resp.json").write_bytes(response)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "masking_rules": rules,
                "pairs": [
                    {
                        "identity": {"method": "GET", "path": "/v1/plans"},
                        "request": "req.json",
                        "response": "resp.json",
                        "cleanup": "none",
                        "sha256": {
                            "request": hashlib.sha256(request).hexdigest(),
                            "response": hashlib.sha256(response).hexdigest(),
                        },
                    }
                ],
            }
        )
    )


async def test_matching_synthetic_pair_replays_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_corpus(tmp_path, PLANS_BODY, rules=[])
    monkeypatch.setenv(CORPUS_ENV, str(tmp_path))
    await plans_replay.replay_recorded_pairs()


async def test_unmasked_difference_fails_and_a_named_mask_absorbs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drifted = json.loads(json.dumps(PLANS_BODY))
    # The app re-emits a UTC instant as ...Z, so a recording that spells it +00:00 differs.
    drifted[0]["created_at"] = "2025-01-15T12:00:00+00:00"
    _write_corpus(tmp_path, drifted, rules=[])
    monkeypatch.setenv(CORPUS_ENV, str(tmp_path))
    with pytest.raises(AssertionError, match="outside the masking rules"):
        await plans_replay.replay_recorded_pairs()
    _write_corpus(
        tmp_path,
        drifted,
        rules=[{"name": "created-at-format", "path": "response.body.*.created_at"}],
    )
    await plans_replay.replay_recorded_pairs()


async def test_tampered_response_file_fails_its_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_corpus(tmp_path, PLANS_BODY, rules=[])
    (tmp_path / "resp.json").write_text('{"status": 200, "body": []}')
    monkeypatch.setenv(CORPUS_ENV, str(tmp_path))
    with pytest.raises(AssertionError, match="sha256"):
        await plans_replay.replay_recorded_pairs()
