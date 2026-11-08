# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The replay machinery itself, against synthetic pairs (not the T18b corpus).

These pairs are written by the test from a live response of the mounted app, so
they prove the comparison catches an unmasked difference and honours a masking
rule. They are not evidence for AC3, which needs the recorded corpus.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.nodes import envelope_example_replay as replay_support
from tests.nodes import envelope_example_support as support

pytestmark = pytest.mark.unit


def _recorded(route: support.Route) -> dict[str, Any]:
    live = TestClient(route.factory()()).request(route.method, route.path)
    return {"status": live.status_code, "json": live.json()}


def _write(
    tmp_path: Path, route: support.Route, response: dict[str, Any], masking: list[str]
) -> Path:
    manifest = {
        "pairs": [
            {
                "identity": f"{route.method} {route.path}",
                "request": {"headers": {}},
                "response": response,
                "masking": masking,
            }
        ]
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


MASK = ["envelope_id", "correlation_id", "timestamp", "payload.tenant_id"]


@pytest.mark.parametrize("route", support.ROUTES, ids=lambda r: r.method)
def test_matching_pair_with_named_masking_replays_clean(
    route: support.Route, tmp_path: Path
) -> None:
    corpus = _write(tmp_path, route, _recorded(route), MASK)
    (pair,) = replay_support.load_pairs(corpus, route)
    assert replay_support.replay(route, pair) == []


@pytest.mark.parametrize("route", support.ROUTES, ids=lambda r: r.method)
def test_unmasked_difference_fails(route: support.Route, tmp_path: Path) -> None:
    response = _recorded(route)
    response["json"]["payload"]["tenant_slug"] = "recorded-elsewhere"
    corpus = _write(tmp_path, route, response, MASK)
    (pair,) = replay_support.load_pairs(corpus, route)
    assert replay_support.replay(route, pair)


@pytest.mark.parametrize("route", support.ROUTES, ids=lambda r: r.method)
def test_unmasked_volatile_field_fails(route: support.Route, tmp_path: Path) -> None:
    corpus = _write(tmp_path, route, _recorded(route), [])
    (pair,) = replay_support.load_pairs(corpus, route)
    assert replay_support.replay(route, pair)


@pytest.mark.parametrize("route", support.ROUTES, ids=lambda r: r.method)
def test_status_difference_fails(route: support.Route, tmp_path: Path) -> None:
    response = _recorded(route)
    response["status"] = 500
    corpus = _write(tmp_path, route, response, MASK)
    (pair,) = replay_support.load_pairs(corpus, route)
    assert any("status" in item for item in replay_support.replay(route, pair))


def test_only_the_routes_own_identity_is_selected(tmp_path: Path) -> None:
    corpus = _write(tmp_path, support.GET_ROUTE, _recorded(support.GET_ROUTE), MASK)
    assert replay_support.load_pairs(corpus, support.POST_ROUTE) == []
    assert len(replay_support.load_pairs(corpus, support.GET_ROUTE)) == 1


def test_file_pair_is_checked_against_its_sha256(tmp_path: Path) -> None:
    route = support.POST_ROUTE
    (tmp_path / "response.yaml").write_text(yaml.safe_dump(_recorded(route)))
    manifest = {
        "pairs": [
            {
                "identity": {"method": route.method, "path": route.path},
                "request": {"headers": {}},
                "response": {"file": "response.yaml"},
                "sha256": "0" * 64,
                "masking": MASK,
            }
        ]
    }
    (tmp_path / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match="sha256"):
        replay_support.load_pairs(tmp_path, route)


def test_unreadable_manifest_is_a_failure_not_a_skip(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps({"pairs": "nope"}))
    with pytest.raises(ValueError, match="no list of pairs"):
        replay_support.load_pairs(tmp_path, support.POST_ROUTE)
    with pytest.raises(ValueError, match="no manifest"):
        replay_support.load_pairs(tmp_path / "missing", support.POST_ROUTE)


def test_skip_reasons_name_t18b_and_the_ticket() -> None:
    for reason in (replay_support.SKIP_REASON_UNSET, replay_support.SKIP_REASON_NONE):
        assert "T18b" in reason
        assert "OMN-22158" in reason
