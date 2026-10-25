# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep agent dispatch on the lab while eligible lab slots are free (OMN-20894)."""

from __future__ import annotations

import json
import re
import socket
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models.model_hook_verdict import ModelHookVerdict
from ..models.model_lab_dispatch_request import LabPool, ModelLabDispatchRequest

MAC_DESTINATIONS = frozenset({"mac", "local", "launching-host"})
DISPATCH_GUARD = "operator_access_mode_lab_dispatch_gate"
RUNNER_COMMAND = (
    'python3 "${CLAUDE_PLUGIN_ROOT}/skills/remote-lane/scripts/onex_remote_lane.py" '
    "--brief <brief file> --lane <lane> --ticket <OMN-n> --model sonnet "
    "--parent <orchestrator lane> --"
)


def route_destinations(text: str) -> tuple[str, ...]:
    return tuple(
        match.group(1).lower()
        for route in re.finditer(r"\bROUTE:([^\r\n]*)", text)
        for match in re.finditer(r"\bdest=([A-Za-z0-9_.-]+)", route.group(1))
    )


def dispatch_text(tool_name: str, tool_input: Mapping[str, Any]) -> str:
    if tool_name == "Agent":
        return "\n".join(
            value
            for key in ("prompt", "description", "label")
            if isinstance(value := tool_input.get(key), str)
        )
    if tool_name != "Workflow":
        return ""
    script = tool_input.get("script")
    if isinstance(script, str):
        return script
    path = tool_input.get("scriptPath")
    if not isinstance(path, str) or not path or not Path(path).is_file():
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


def _free_slots(host: Mapping[str, Any], excluded: set[str], now: datetime) -> int:
    name = str(host["host_name"]).split(".")[0].lower()
    if name in excluded:
        return 0
    advertised = datetime.fromisoformat(str(host["advertised_at"]))
    age = (now - advertised).total_seconds()
    cadence = float(host["cadence_seconds"])
    cores = int(host["cores"])
    running = int(host["running_units"])
    maximum = int(host["max_units"])
    if (
        cadence <= 0
        or abs(age) > 2 * cadence
        or cores <= 0
        or running < 0
        or (float(host["load1"]) + running) / cores > 0.75
        or int(host["mem_available_bytes"]) < 4 * 1024**3
    ):
        return 0
    return max(0, maximum - running)


def read_lab_pool(
    environ: Mapping[str, str],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    now: datetime | None = None,
) -> LabPool:
    python = environ.get("ONEX_LAB_WORK_PYTHON", "").strip()
    venv = environ.get("ONEX_DISPATCH_VENV", "").strip()
    if not python and venv:
        python = str(Path(venv) / "bin" / "python")
    home = environ.get("OMNI_HOME", "").strip()
    if not python or not home:
        return LabPool(None, "lab pool Python or OMNI_HOME is not configured")
    command = [
        python,
        "-m",
        "omnimarket.lab_work.cli",
        "hosts",
        "--omnibase-path",
        home,
        "--json",
        "--window",
        "3",
    ]
    lane = environ.get("ONEX_LAB_POOL_BUS_LANE", "").strip()
    if lane:
        command.extend(["--bus-lane", lane])
    try:
        result = run(command, capture_output=True, text=True, timeout=6, check=False)
        if result.returncode:
            return LabPool(None, "lab pool hosts command failed")
        hosts: object = json.loads(result.stdout)
        if not isinstance(hosts, list):
            return LabPool(None, "lab pool hosts response is not a list")
        excluded = {
            "mac",
            socket.gethostname().split(".")[0].lower(),
            *(
                name.strip().split(".")[0].lower()
                for name in environ.get("ONEX_LAB_POOL_LAUNCHING_HOSTS", "").split(",")
            ),
        }
        free = 0
        evaluated_at = now or datetime.now(UTC)
        for host in hosts:
            if not isinstance(host, dict):
                return LabPool(None, "lab pool host response is not an object")
            free += _free_slots(host, excluded, evaluated_at)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        return LabPool(None, "lab pool capacity could not be read")
    return LabPool(free, f"{free} free slots on eligible lab hosts")


def mac_route_count(tool_name: str, tool_input: Mapping[str, Any]) -> int:
    """How many ROUTE lines of this Workflow or Agent call name the launching Mac."""
    if tool_name not in {"Workflow", "Agent"}:
        return 0
    return sum(
        destination in MAC_DESTINATIONS
        for destination in route_destinations(dispatch_text(tool_name, tool_input))
    )


class HandlerLabDispatchGate:
    """Refuse a Mac route when the lab has capacity, in every session and lane."""

    def handle(self, request: ModelLabDispatchRequest) -> ModelHookVerdict:
        free = request.pool.free_slots
        if free is None or free <= 0:
            return ModelHookVerdict.allow()
        calls = mac_route_count(request.tool_name, request.tool_input)
        if not calls:
            return ModelHookVerdict.allow()
        return ModelHookVerdict.refuse(
            f"BLOCKED (lab-only agents, OMN-20894): {free} free "
            f"slot{'' if free == 1 else 's'} in the lab; "
            f"{calls} agent calls route to dest=mac (or local/launching-host). "
            f"Use dest=lab and the remote-lane runner: {RUNNER_COMMAND}",
            DISPATCH_GUARD,
            "mac-dispatch-lab-free",
        )
