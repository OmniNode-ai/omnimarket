# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The placement node answers a stopped engine the way the remote-lane runner asks it (OMN-21958).

The runner asks the placement node through the canonical runtime, ``onex node <name> --input <file> --output
receipt`` (onex_remote_lane.py, ``node_decision``), and acts on the typed answer. This test runs that exact command
over a request carrying a fixture stop: the answer places no host and names the stop and its swarm lane; the same
request with no stop open places. The runner's own print of the refusal line is the plugin's side of the change.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.nodes.node_andon_stop_reducer.stop_gate_facts import SWARM, facts, stop

pytestmark = pytest.mark.unit

REQUEST: dict[str, Any] = {
    "engine": "claude_opus",
    "escalation_reason": "complexity:B4",
    "readings": [
        {
            "name": "host-a",
            "engines": ["claude_opus"],
            "lane_slots": 2,
            "lane_cap": 4,
            "rank_free": 5.0,
        }
    ],
}


def ask(tmp_path: Path, name: str, *stops: Any) -> tuple[int, dict[str, Any]]:
    gate = facts(tmp_path / name, "enforce", *stops)
    request = tmp_path / f"{name}.json"
    request.write_text(
        json.dumps({**REQUEST, "stop_gate": json.loads(gate.model_dump_json())}),
        encoding="utf-8",
    )
    done = subprocess.run(
        [
            str(Path(sys.executable).parent / "onex"),
            "node",
            "node_remote_lane_compute",
            "--input",
            str(request),
            "--output",
            "receipt",
            "--state-root",
            str(tmp_path / "state"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )
    receipts = [
        json.loads(line)
        for line in done.stdout.splitlines()
        if line.startswith('{"skill_name"')
    ]
    assert receipts, done.stderr[-2000:]
    return done.returncode, receipts[-1]["result"]


def test_stop_gate_runner_path_a_stopped_engine_places_nothing_and_names_the_stop(
    tmp_path: Path,
) -> None:
    code, answer = ask(tmp_path, "stopped", stop())
    assert code == 0
    assert answer["host"] is None
    assert answer["outcome"] == "STOPPED"
    assert stop().id in answer["refusal"]
    assert f"swarm={SWARM}" in answer["refusal"]


def test_stop_gate_runner_path_the_same_request_with_no_stop_places(
    tmp_path: Path,
) -> None:
    code, answer = ask(tmp_path, "open")
    assert code == 0
    assert answer["host"] == "host-a"
    assert answer["outcome"] == "ADMITTED"
    assert answer["refusal"] is None
