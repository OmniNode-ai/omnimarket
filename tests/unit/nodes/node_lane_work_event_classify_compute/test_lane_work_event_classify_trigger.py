# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The classifier declares a real trigger, and the declared command runs it (OMN-26707).

The node-trigger-declaration check refuses a changed node that nothing runs. The node
declares ``runtime_dispatch.on_demand``, and the command it names must answer a request
on stdin, or the declaration would name a command that does nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_trigger_declaration_check_effect.handlers.handler_trigger_declaration_check import (
    HandlerTriggerDeclarationCheck,
)
from omnimarket.nodes.node_trigger_declaration_check_effect.models.model_trigger_declaration_check import (
    ModelTriggerCheckRequest,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
NODE = "node_lane_work_event_classify_compute"
CONTRACT = f"src/omnimarket/nodes/{NODE}/contract.yaml"
MODULE = f"omnimarket.nodes.{NODE}"


def test_trigger_check_accepts_the_contract() -> None:
    result = HandlerTriggerDeclarationCheck().handle(
        ModelTriggerCheckRequest(root=str(ROOT), contracts=(CONTRACT,))
    )
    assert result.needing_trigger == 1
    assert result.findings == ()


def test_declared_command_is_the_module_and_names_an_owner() -> None:
    contract = yaml.safe_load((ROOT / CONTRACT).read_text(encoding="utf-8"))
    on_demand = contract["runtime_dispatch"]["on_demand"]
    assert f"python -m {MODULE}" in on_demand["command"]
    assert on_demand["owner"]


def test_declared_command_answers_a_request_on_stdin() -> None:
    request = {
        "state": {
            "workflow_id": "wf-trigger-1",
            "change": {
                "owner": "OmniNode-ai",
                "repo": "omnimarket",
                "pr_number": 6904,
                "ticket": "OMN-26043",
            },
            "state": "pushing",
            "seq": 1,
            "worker_lane": "lane-1",
        },
        "result_kind": "raw_text",
        "raw_result": "zzqq nothing here matches any rule",
    }
    done = subprocess.run(
        [sys.executable, "-m", MODULE],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert done.returncode == 0, done.stderr
    answer = json.loads(done.stdout)
    assert answer["outcome"] == "event"
    assert answer["event"]["kind"] == "unclassified"
