# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The replay machinery itself, against a synthetic corpus: it must be able to fail."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.nodes.inference_credential_http_support import ROUTE
from tests.nodes.t18b_replay_core import CORPUS_ENV, run_replay

pytestmark = pytest.mark.unit


def _corpus(tmp_path: Path, response_body: object, status: int = 200) -> Path:
    manifest = {
        "masking_rules": [{"name": "created-at", "field": "created_at"}],
        "pairs": [
            {
                "id": "get-1",
                "identity": f"GET {ROUTE}",
                "request": {"path": ROUTE, "headers": {"x-api-key": "recorded"}},
                "response": {"status": status, "body": response_body},
                "setup": {
                    "credentials": [
                        {"api_key_ref": "cred_a", "name": "k", "provider": "openai"}
                    ]
                },
            }
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def test_a_matching_pair_passes_with_the_masked_field_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = [
        {
            "api_key_ref": "cred_a",
            "name": "k",
            "provider": "openai",
            "created_at": "1999-01-01T00:00:00Z",
            "revoked_at": None,
        }
    ]
    monkeypatch.setenv(CORPUS_ENV, str(_corpus(tmp_path, body)))
    run_replay("GET", ROUTE)


def test_an_unmasked_difference_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = [
        {
            "api_key_ref": "cred_a",
            "name": "DIFFERENT",
            "provider": "openai",
            "created_at": "x",
            "revoked_at": None,
        }
    ]
    monkeypatch.setenv(CORPUS_ENV, str(_corpus(tmp_path, body)))
    with pytest.raises(AssertionError):
        run_replay("GET", ROUTE)


def test_unset_or_empty_corpus_skips_naming_t18b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CORPUS_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception, match="T18b, OMN-22158"):
        run_replay("GET", ROUTE)
    (tmp_path / "manifest.json").write_text(json.dumps({"pairs": []}))
    monkeypatch.setenv(CORPUS_ENV, str(tmp_path))
    with pytest.raises(pytest.skip.Exception, match="T18b, OMN-22158"):
        run_replay("GET", ROUTE)
