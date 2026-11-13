# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the GPU power caps node tests (OMN-24042): a settings document and a fake nvidia-smi."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_gpu_power_caps_effect.handlers.handler_gpu_power_caps import (
    HandlerGpuPowerCaps,
    select_backend,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps_overlay import (
    ModelGpuPowerCapsOverlay,
)
from omnimarket.nodes.node_host_installer_net_units.models.model_net_units import (
    ModelNetUnitsOverlay,
)

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_gpu_power_caps_effect"
)
SIM_A = "sim-host-a"
BIG = "Sim Card Big"
SMALL = "Sim Card Small"


def in_memory_overlay() -> ModelGpuPowerCapsOverlay:
    text = (NODE_DIR / "overlays/in_memory.yaml").read_text(encoding="utf-8")
    return ModelGpuPowerCapsOverlay.model_validate(yaml.safe_load(text))


def unit_entry(host: str, values: dict[str, str]) -> dict[str, Any]:
    return {
        "kind": "systemd-system-service",
        "platform": "linux",
        "hosts": [host],
        "service": "gpu-power-caps.service",
        "files": [
            {
                "content": "x",
                "destination": "/etc/systemd/system/gpu-power-caps.service",
                "mode": "0644",
                "render": "values",
                "sha256": "0" * 64,
                "values": dict(values),
            }
        ],
    }


def caps_document() -> dict[str, Any]:
    """The settings document in the shape the overlays repository declares it, for the two simulated hosts."""
    return {
        "schema": "host_net_units.v1",
        "units": {
            "gpu-power-caps-sim-a": unit_entry(
                SIM_A,
                {
                    "GPU_NAME_0": BIG,
                    "WATTS_0": "500",
                    "GPU_NAME_1": SMALL,
                    "WATTS_1": "300",
                },
            ),
            "gpu-power-caps-sim-b": unit_entry(
                "sim-host-b", {"GPU_NAME_0": SMALL, "WATTS_0": "300"}
            ),
        },
    }


def values_of(doc: dict[str, Any], unit: str) -> dict[str, str]:
    values: dict[str, str] = doc["units"][unit]["files"][0]["values"]
    return values


def build(
    doc: dict[str, Any], overlay: ModelGpuPowerCapsOverlay | None = None
) -> tuple[HandlerGpuPowerCaps, Any]:
    """A handler over the in-memory backend that re-reads ``doc`` on every request."""
    overlay = overlay or in_memory_overlay()
    backend = select_backend(overlay)
    handler = HandlerGpuPowerCaps(
        overlay,
        backend,
        caps_loader=lambda: ModelNetUnitsOverlay.model_validate(copy.deepcopy(doc)),
    )
    return handler, backend


# A fake nvidia-smi: state in a JSON file beside the script; the query prints one csv line per card the way the
# real tool does with ``--format=csv,noheader,nounits``; ``-i N -pl W`` rewrites the limit.
FAKE_SMI = """\
import json, sys
state_path = sys.argv[0] + ".json"
cards = json.load(open(state_path))
args = sys.argv[1:]
if args and args[0].startswith("--query-gpu="):
    for c in cards:
        print(f"{c['index']}, {c['name']}, {c['limit']:.2f}, {c['min']:.2f}, {c['max']:.2f}")
elif args[:1] == ["-i"] and args[2:3] == ["-pl"]:
    for c in cards:
        if str(c["index"]) == args[1]:
            c["limit"] = float(args[3])
    json.dump(cards, open(state_path, "w"))
    print("Power limit set")
else:
    sys.exit(2)
"""


def fake_smi(tmp_path: Path) -> Path:
    script = tmp_path / "fake_smi.py"
    script.write_text(FAKE_SMI, encoding="utf-8")
    cards = [
        {"index": 0, "name": BIG, "limit": 600.0, "min": 100.0, "max": 600.0},
        {"index": 1, "name": SMALL, "limit": 450.0, "min": 100.0, "max": 450.0},
    ]
    Path(str(script) + ".json").write_text(json.dumps(cards), encoding="utf-8")
    return script


def subprocess_overlay(
    script: Path,
    *,
    read_program: list[str] | None = None,
    set_program: list[str] | None = None,
    transport_prefix: list[str] | None = None,
) -> ModelGpuPowerCapsOverlay:
    base = yaml.safe_load(
        (NODE_DIR / "overlays/in_memory.yaml").read_text(encoding="utf-8")
    )
    py = [sys.executable, str(script)]
    base["backend"] = "subprocess"
    base["read_argv"] = [
        *(read_program or py),
        "--query-gpu=index,name,power.limit,power.min_limit,power.max_limit",
        "--format=csv,noheader,nounits",
    ]
    base["set_argv"] = [*(set_program or py), "-i", "{index}", "-pl", "{watts}"]
    for host in base["hosts"]:
        host["transport_prefix"] = transport_prefix or []
        host["simulated_cards"] = []
    return ModelGpuPowerCapsOverlay.model_validate(base)


def state_limits(script: Path) -> dict[int, float]:
    raw = json.loads(Path(str(script) + ".json").read_text(encoding="utf-8"))
    return {c["index"]: c["limit"] for c in raw}
