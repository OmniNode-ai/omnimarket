# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18931: the bounded delegation lanes are declared here and ship in the wheel.

omnibase_infra's runtime reads ``lanes.dogfood.delegation_fault_routes``,
``lanes.dogfood.broker_topology`` and ``lanes.<dev|dogfood>.delegation_routes``
from the INSTALLED omnimarket package resource
``omnimarket/config/ci_bus_lanes.yaml`` and fails closed when they are absent.
These tests pin the declared values that infra's validators accept, and pin the
packaging that puts the one checked-in copy at that resource path.
"""

from __future__ import annotations

import subprocess
import tomllib
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_OVERLAY = _REPO / "config" / "ci_bus_lanes.yaml"
_RESOURCE = "omnimarket/config/ci_bus_lanes.yaml"
_ROUTE = {
    "consumer": "omnimarket.nodes.node_delegation_orchestrator",
    "terminal_route": "terminal_events",
    "repository_owner": "omnimarket",
}


def _lanes() -> dict[str, dict[str, object]]:
    lanes = yaml.safe_load(_OVERLAY.read_text(encoding="utf-8"))["lanes"]
    assert isinstance(lanes, dict)
    return lanes


def test_dogfood_declares_exactly_the_two_fixed_status_fault_routes() -> None:
    routes = _lanes()["dogfood"]["delegation_fault_routes"]
    assert isinstance(routes, list)
    assert sorted(route["expected_http_status"] for route in routes) == [429, 503]
    assert len({route["backend_id"] for route in routes}) == len(routes)
    for route in routes:
        status = route["expected_http_status"]
        endpoint = urlsplit(route["endpoint_url"])
        assert (endpoint.scheme, endpoint.hostname, endpoint.port, endpoint.path) == (
            "http",
            f"dogfood-delegation-fault-{status}",
            8080,
            "/v1/chat/completions",
        )
        assert route["requested_timeout_seconds"] == 240
        assert route["max_attempts"] == 1
        assert route["no_escalation"] is True


def test_dogfood_topology_external_member_is_the_declared_broker() -> None:
    dogfood = _lanes()["dogfood"]
    topology = dogfood["broker_topology"]
    assert isinstance(topology, dict)
    assert topology["external_bootstrap_servers"] == dogfood["broker"]
    assert topology["internal_bootstrap_servers"] == "redpanda:9092"


@pytest.mark.parametrize("lane", ["dev", "dogfood"])
def test_each_bounded_lane_declares_exactly_one_delegation_route(lane: str) -> None:
    assert _lanes()[lane]["delegation_routes"] == [_ROUTE]


@pytest.mark.parametrize("lane", ["stability", "stability-test", "prod", "ci-bus"])
def test_no_other_lane_declares_a_delegation_or_fault_route(lane: str) -> None:
    declaration = _lanes()[lane]
    assert "delegation_routes" not in declaration
    assert "delegation_fault_routes" not in declaration
    assert "broker_topology" not in declaration


def test_the_lane_declaration_is_packaged_from_its_one_checked_in_copy() -> None:
    project = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    forced = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert forced == {"config/ci_bus_lanes.yaml": _RESOURCE}
    assert not (_REPO / "src" / _RESOURCE).exists()


def test_the_built_wheel_carries_the_declaration_byte_for_byte(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(_REPO)],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        assert wheel.read(_RESOURCE) == _OVERLAY.read_bytes()
