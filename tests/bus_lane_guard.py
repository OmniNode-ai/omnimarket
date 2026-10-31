# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The bus lane a test may reach, and the refusal for any other (OMN-17427).

The broker for a test comes from the declared lane in ``config/test_bus_lane.yaml``
resolved through the lane overlay ``config/ci_bus_lanes.yaml``. It never comes
from ``KAFKA_BOOTSTRAP_SERVERS`` in the process environment: an operator machine
exports that variable for its own tooling, and a suite that read it published
test events to another team's lane. A broker that belongs to a declared lane
other than the test lane is refused before anything is published.

Lane identity is the broker port. The overlay names the same host differently
(tailnet name, LAN address) while every lane has its own port, so a port match
holds where a host-name match would miss.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_BUS_LANE_PATH = _REPO_ROOT / "config" / "test_bus_lane.yaml"


class BusLaneRefusalError(RuntimeError):
    """A test tried to reach a bus lane other than the declared test lane."""


def _load_ci_bus_lanes() -> ModuleType:
    """Load ``scripts/ci_bus_lanes.py``; ``scripts/`` is not a package."""
    path = _REPO_ROOT / "scripts" / "ci_bus_lanes.py"
    spec = importlib.util.spec_from_file_location("ci_bus_lanes", path)
    if spec is None or spec.loader is None:
        raise BusLaneRefusalError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("ci_bus_lanes", module)
    spec.loader.exec_module(module)
    return module


_lanes = _load_ci_bus_lanes()
LANE_OVERLAY_PATH: Path = _lanes.LANE_OVERLAY_PATH
MODE_CONCRETE: str = _lanes.MODE_CONCRETE
load_lane_overlay = _lanes.load_lane_overlay
resolve_lane_broker = _lanes.resolve_lane_broker


def declared_test_lane(path: Path = TEST_BUS_LANE_PATH) -> str:
    """Return the lane name declared in the test-lane overlay, or refuse."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    lane = loaded.get("lane") if isinstance(loaded, dict) else None
    if not isinstance(lane, str) or not lane.strip():
        raise BusLaneRefusalError(f"{path} declares no test bus lane")
    return lane.strip()


def _port(broker: str) -> str:
    return broker.rsplit(":", 1)[-1].strip()


def _concrete_lane_ports(overlay: dict[str, object]) -> dict[str, str]:
    """Map every concrete overlay lane name to its broker port."""
    lanes = overlay.get("lanes")
    names = list(lanes) if isinstance(lanes, dict) else []
    ports: dict[str, str] = {}
    for name in names:
        mode, broker = resolve_lane_broker(overlay, str(name))
        if mode == MODE_CONCRETE:
            ports[str(name)] = _port(broker)
    return ports


def resolve_test_bus_broker(
    *,
    declaration_path: Path = TEST_BUS_LANE_PATH,
    overlay_path: Path = LANE_OVERLAY_PATH,
) -> str:
    """Return the declared test lane's broker. The environment is never read."""
    lane = declared_test_lane(declaration_path)
    mode, broker = resolve_lane_broker(load_lane_overlay(overlay_path), lane)
    if mode != MODE_CONCRETE:
        raise BusLaneRefusalError(
            f"test bus lane {lane!r} resolves to {mode!r}, not a concrete broker"
        )
    return broker


def assert_broker_in_declared_lane(
    broker: str,
    *,
    declaration_path: Path = TEST_BUS_LANE_PATH,
    overlay_path: Path = LANE_OVERLAY_PATH,
) -> str:
    """Refuse a broker outside the declared test lane; return it when it is inside.

    Call this before the first connection or publish. A port that no declared
    lane owns is refused too: an undeclared broker is not a lane the test is
    allowed to assume.
    """
    lane = declared_test_lane(declaration_path)
    ports = _concrete_lane_ports(load_lane_overlay(overlay_path))
    if lane not in ports:
        raise BusLaneRefusalError(f"test bus lane {lane!r} has no concrete broker")
    port = _port(broker)
    if port == ports[lane]:
        return broker
    owner = next((name for name, p in ports.items() if p == port), None)
    where = f"belongs to lane {owner!r}" if owner else "belongs to no declared lane"
    raise BusLaneRefusalError(
        f"refusing broker {broker!r}: it {where}, and this test is declared "
        f"for lane {lane!r} (config/test_bus_lane.yaml). Nothing was published."
    )
