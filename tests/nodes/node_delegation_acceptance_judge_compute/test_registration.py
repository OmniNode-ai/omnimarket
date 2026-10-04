# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registered acceptance judge resolves to the typed compute handler and runs under onex node."""

import json
import subprocess
import sys
import tomllib
from importlib import import_module
from pathlib import Path

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.handler_delegation_acceptance_judge import (
    HandlerDelegationAcceptanceJudge,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_judge_request import (
    ModelAcceptanceJudgeRequest,
)
from tests.nodes.node_delegation_acceptance_judge_compute.builders import (
    RUBRIC_YAML,
    entry,
    reply,
)

pytestmark = pytest.mark.unit

NODE = "node_delegation_acceptance_judge_compute"


def test_registered_acceptance_judge_route() -> None:
    route = _resolve_node_route(NODE)
    assert (
        route.command_topic
        == "onex.cmd.omnimarket.delegation-acceptance-judge-requested.v1"
    )
    assert route.terminal_topic == "onex.evt.omnimarket.delegation-acceptance-judged.v1"
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationAcceptanceJudge
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelAcceptanceJudgeRequest
    )


def test_node_is_an_entry_point_in_pyproject() -> None:
    root = Path(__file__).resolve().parents[3]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    points = pyproject["project"]["entry-points"]["onex.nodes"]
    assert points[NODE] == f"omnimarket.nodes.{NODE}"


def test_node_runs_under_onex_node_from_a_request_file(tmp_path: Path) -> None:
    """The skill drives the node with ``onex node``; no command line of its own exists."""
    onex = Path(sys.executable).parent / "onex"
    if not onex.is_file():
        pytest.skip(f"no onex entry point beside {sys.executable}")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "operation": "check",
                "rubric_yaml": RUBRIC_YAML,
                "batch_item_ids": ["a"],
                "reply_text": reply(entry("a")),
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            str(onex),
            "node",
            NODE,
            "--input",
            str(request),
            "--state-root",
            str(tmp_path / "state"),
            "--output",
            "receipt",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    receipt = json.loads(proc.stdout.strip().splitlines()[-1])
    assert receipt["status"] == "success"
    assert receipt["result"]["status"] == "passed"
    assert receipt["result"]["verdicts"][0]["item_id"] == "a"
