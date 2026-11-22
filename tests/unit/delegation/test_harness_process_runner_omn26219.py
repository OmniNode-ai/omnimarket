# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The process and bus seams of the harness handler: runner receipts and contract wiring (OMN-26219)."""

from __future__ import annotations

import sys
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_backend_call import (
    HandlerHarnessBackendCall,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_delegation import (
    HandlerHarnessDelegation,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_process_runner import (
    HandlerHarnessProcessRunner,
)
from tests.unit.delegation.harness_support_omn26219 import (
    AcceptAllGate,
    ListCostSink,
    ScriptedRunner,
    call_request,
    load_overlay,
    receipt,
)

pytestmark = pytest.mark.unit

PACKAGE = "omnimarket.nodes.node_llm_delegation_call_effect"


class Environment:
    def __init__(self, path: Path) -> None:
        self._path = path

    def environment(self) -> dict[str, str]:
        import os

        return {"PATH": f"{self._path}:{os.environ['PATH']}", "HOME": str(self._path)}


def fake_claude(directory: Path, body: str) -> None:
    script = directory / "claude"
    script.write_text(f"#!{sys.executable}\n" + body)
    script.chmod(0o700)


def claude_backend() -> ModelDelegationBackendConfig:
    return ModelDelegationBackendConfig(
        backend_id="harness-test-claude",
        provider="example-provider",
        kind="harness",
        harness="claude",
        surface="internal",
        tenant_scope="house",
        model_name="example-model",
        tier="harness",
    )


async def test_harness_process_runner_reads_the_result_record(tmp_path: Path) -> None:
    fake_claude(
        tmp_path,
        """import json, sys
args = sys.argv[1:]
assert args[args.index("--model") + 1] == "example-model"
assert "--output-format" in args
assert sys.stdin.read() == "the prompt"
print(json.dumps({"type": "result", "result": "the answer", "is_error": False,
                  "total_cost_usd": 0.0123, "usage": {"input_tokens": 11, "output_tokens": 5}}))
""",
    )
    runner = HandlerHarnessProcessRunner(environment=Environment(tmp_path), agents={})
    got = await runner.run(claude_backend(), "the prompt", 20)
    assert got.exit_cause == "completed"
    assert got.answer == "the answer"
    assert (got.tokens_in, got.tokens_out) == (11, 5)
    assert got.cost_usd == Decimal("0.0123")


async def test_harness_process_runner_missing_usage_is_unknown_not_zero(
    tmp_path: Path,
) -> None:
    fake_claude(
        tmp_path,
        'import json, sys\nsys.stdin.read()\nprint(json.dumps({"type": "result", "result": "x"}))\n',
    )
    runner = HandlerHarnessProcessRunner(environment=Environment(tmp_path), agents={})
    got = await runner.run(claude_backend(), "p", 20)
    assert got.exit_cause == "completed"
    assert (got.tokens_in, got.tokens_out, got.cost_usd) == (None, None, None)


async def test_harness_process_runner_error_result_is_a_failed_turn(
    tmp_path: Path,
) -> None:
    fake_claude(
        tmp_path,
        'import json, sys\nsys.stdin.read()\nprint(json.dumps({"type": "result", "result": "boom", "is_error": True}))\n',
    )
    runner = HandlerHarnessProcessRunner(environment=Environment(tmp_path), agents={})
    got = await runner.run(claude_backend(), "p", 20)
    assert got.exit_cause == "turn_failed"
    assert got.answer == ""


async def test_harness_process_runner_deadline_is_a_timeout(tmp_path: Path) -> None:
    fake_claude(tmp_path, "import sys, time\nsys.stdin.read()\ntime.sleep(30)\n")
    runner = HandlerHarnessProcessRunner(environment=Environment(tmp_path), agents={})
    got = await runner.run(claude_backend(), "p", 0.5)
    assert got.exit_cause == "timeout"


async def test_harness_process_runner_glm_without_declared_route_does_not_launch(
    tmp_path: Path,
) -> None:
    """GLM runs only through Claude Code with a declared provider route and credential reference."""
    fake_claude(tmp_path, 'raise SystemExit("must not be launched")\n')
    glm = load_overlay().backend_by_id()["harness-claude-glm-flash"]
    runner = HandlerHarnessProcessRunner(environment=Environment(tmp_path), agents={})
    got = await runner.run(glm, "p", 20)
    assert got.exit_cause == "launch_failed"


async def test_harness_delegation_handler_delegates_to_its_bound_call() -> None:
    sink = ListCostSink()
    call = HandlerHarnessBackendCall(
        runner=ScriptedRunner([receipt("def add(a, b):\n    return a + b\n")]),
        gate=AcceptAllGate(),
        cost_sink=sink,
        overlay=load_overlay(),
    )
    handler = HandlerHarnessDelegation(call=call)
    result = await handler.handle(call_request("code_generation"))
    assert (result.status, result.ok) == ("completed", True)
    assert len(sink.records) == 1


def test_harness_delegation_is_routed_by_the_contract() -> None:
    contract: dict[str, Any] = yaml.safe_load(
        resources.files(PACKAGE).joinpath("contract.yaml").read_text()
    )
    routed = {h["handler"]["name"]: h for h in contract["handler_routing"]["handlers"]}
    entry = routed["HandlerHarnessDelegation"]
    assert entry["event_model"]["name"] == "ModelHarnessBackendCallRequest"
    topic = "onex.cmd.omnimarket.delegation-harness-execute.v1"
    assert topic in contract["event_bus"]["subscribe_topics"]
    block = contract["harness_backend_call"]
    assert block["attempt_topic"] in contract["event_bus"]["publish_topics"]
    assert block["agents_call_site"] in contract["delegation_adapters"]["call_sites"]
    published = {e["event_type"]: e["topic"] for e in contract["published_events"]}
    assert (
        published["HarnessBackendCallResult"] in contract["event_bus"]["publish_topics"]
    )
