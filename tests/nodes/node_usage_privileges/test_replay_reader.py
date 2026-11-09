# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Positive and negative controls for the privileges replay, on a synthetic corpus.

The pairs are written to a temporary directory and are not T18b data; they prove the
replay runs and fails on a difference when pairs are present.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.nodes.node_plan_list.replay_corpus import CORPUS_ENV
from tests.nodes.node_usage_privileges import test_t18b_replay as privileges_replay

pytestmark = pytest.mark.unit
IDENTITY = {"method": "GET", "path": "/v1/usage/privileges"}
BODY = [
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "plan_tier_code": "free",
        "resource_code": "events",
        "monthly_quota": None,
        "hard_limit": True,
        "created_at": "2025-01-15T12:00:00Z",
    }
]


def _write_corpus(root: Path, body: object, status: int = 200) -> None:
    request = json.dumps(
        {
            "method": "GET",
            "path": "/v1/usage/privileges",
            "headers": {"x-api-key": "masked-key"},
        }
    ).encode()
    response = json.dumps(
        {
            "status": status,
            "headers": {"content-type": "application/json"},
            "body": body,
        }
    ).encode()
    (root / "req.json").write_bytes(request)
    (root / "resp.json").write_bytes(response)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "masking_rules": [],
                "pairs": [
                    {
                        "identity": IDENTITY,
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
    _write_corpus(tmp_path, BODY)
    monkeypatch.setenv(CORPUS_ENV, str(tmp_path))
    await privileges_replay.replay_recorded_pairs()


async def test_refusal_pair_replays_clean_and_a_wrong_status_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CORPUS_ENV, str(tmp_path))
    # Recorded 401: the rebuilt resolver accepts nobody, so the credential is refused.
    _write_corpus(
        tmp_path, {"detail": "Unauthorized", "code": "auth.credential.invalid"}, 401
    )
    await privileges_replay.replay_recorded_pairs()
    # A recorded 404 is a status the mount never answers: an unmasked difference.
    _write_corpus(tmp_path, {"detail": "Not Found"}, 404)
    with pytest.raises(AssertionError, match="outside the masking rules"):
        await privileges_replay.replay_recorded_pairs()
