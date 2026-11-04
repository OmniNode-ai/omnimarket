"""Test support: a workspace whose config root holds the lane declaration and the nodes' overlays (OMN-22362).

The architecture-discovery callers dial three nodes, each on the broker its own contract overlay
declares. A test writes the overlays and the lane declaration under ``tmp_path`` and selects that
config root the way a deployment does; broker addresses are ``.test`` names.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

DISCOVERY_NODES = (
    "node_architecture_discovery_orchestrator",
    "node_coding_agent_workspace_compute",
    "node_coding_agent_invoke_effect",
)
LANE_BROKER = "lane-broker.test:19092"
_AMBIENT = (
    "KAFKA_BOOTSTRAP_SERVERS",
    "KAFKA_SECURITY_PROTOCOL",
    "KAFKA_SASL_MECHANISM",
    "KAFKA_SASL_USERNAME",
    "KAFKA_SASL_PASSWORD",
    "OMNI_HOME",
)


def install_broker_overlays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_bus: Mapping[str, object] | None,
    *,
    nodes: tuple[str, ...] = DISCOVERY_NODES,
) -> Path:
    """Write the lane declaration and, for each node, an overlay declaring ``event_bus``.

    ``event_bus=None`` writes the overlay without the key. The ambient broker and SASL variables
    are cleared so a test sets only what it means to. Returns the config root.
    """
    for name in _AMBIENT:
        monkeypatch.delenv(name, raising=False)
    config_root = tmp_path / "config-owner"
    declaration = config_root / "config" / "lab" / "ci_bus_lanes.yaml"
    declaration.parent.mkdir(parents=True, exist_ok=True)
    declaration.write_text(
        yaml.safe_dump(
            {
                "lanes": {
                    "dev": {"broker": LANE_BROKER, "security_protocol": "PLAINTEXT"},
                    "scram": {
                        "broker": LANE_BROKER,
                        "security_protocol": "SASL_PLAINTEXT",
                        "sasl_mechanism": "SCRAM-SHA-256",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    for node in nodes:
        overlay = config_root / "config" / "onex" / "overlays" / node / "overlay.yaml"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        body: dict[str, object] = {"registry_dir": "example"}
        if event_bus is not None:
            body["event_bus"] = dict(event_bus)
        overlay.write_text(yaml.safe_dump(body), encoding="utf-8")
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(config_root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path / "workspace"))
    return config_root
