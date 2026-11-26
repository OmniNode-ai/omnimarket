# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude harness handler: subscription only, declared effort, the model that answered (OMN-26043)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_backend_call import (
    HandlerHarnessBackendCall,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_process_runner import (
    HandlerHarnessProcessRunner,
)
from tests.unit.delegation.harness_support_omn26219 import (
    AcceptAllGate,
    ListCostSink,
    call_request,
    load_overlay,
)

pytestmark = pytest.mark.unit

TIERS = {
    "harness_haiku": ("claude-haiku-5-5", "low"),
    "harness_sonnet": ("claude-sonnet-5-5", "medium"),
    "harness_opus": ("claude-opus-5-5", "high"),
}


class Environment:
    def __init__(self, path: Path, extra: dict[str, str] | None = None) -> None:
        self._path = path
        self._extra = extra or {}

    def environment(self) -> dict[str, str]:
        return {
            "PATH": f"{self._path}:{os.environ['PATH']}",
            "HOME": str(self._path),
            **self._extra,
        }


def fake_claude(directory: Path) -> Path:
    """A claude that records its argv and working directory, then answers as a different model."""
    seen = directory / "seen.json"
    script = directory / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "prompt = sys.stdin.read()\n"
        f"open({str(seen)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'cwd': os.getcwd()}}))\n"
        "print(json.dumps({'type': 'result', 'result': 'ok', 'is_error': False,\n"
        "  'usage': {'input_tokens': 3, 'output_tokens': 2},\n"
        "  'modelUsage': {'claude-haiku-5-5': {'inputTokens': 3, 'outputTokens': 2}}}))\n"
    )
    script.chmod(0o700)
    return seen


def declared(tier: str) -> ModelDelegationBackendConfig:
    overlay = load_overlay()
    return overlay.backend_by_id()[overlay.tier_by_name()[tier].backend_id]


def test_overlay_declares_three_claude_tiers_with_model() -> None:
    for tier, (model, _effort) in TIERS.items():
        backend = declared(tier)
        assert backend.harness is not None
        assert backend.harness.value == "claude"
        assert backend.model_name == model


def test_backend_wire_model_carries_no_effort_field() -> None:
    """A new key on this wire model is refused by the pinned consumer's extra=forbid decode."""
    assert "effort" not in ModelDelegationBackendConfig.model_fields


@pytest.mark.parametrize("tier", sorted(TIERS))
async def test_each_tier_runs_at_its_effort(tmp_path: Path, tier: str) -> None:
    seen = fake_claude(tmp_path)
    runner = HandlerHarnessProcessRunner(environment=Environment(tmp_path), agents={})
    await runner.run(declared(tier), "p", 20)
    args = json.loads(seen.read_text())["argv"]
    assert args[args.index("--effort") + 1] == TIERS[tier][1]


async def test_effort_is_passed_and_the_child_runs_in_a_clean_directory(
    tmp_path: Path,
) -> None:
    seen = fake_claude(tmp_path)
    runner = HandlerHarnessProcessRunner(environment=Environment(tmp_path), agents={})
    got = await runner.run(declared("harness_opus"), "p", 20)
    record = json.loads(seen.read_text())
    args = record["argv"]
    assert args[args.index("--effort") + 1] == "high"
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert not Path(record["cwd"]).exists()  # a temporary directory, removed after
    assert Path(record["cwd"]).resolve() != Path.cwd().resolve()
    assert got.exit_cause == "completed"


async def test_receipt_names_the_model_that_answered(tmp_path: Path) -> None:
    fake_claude(tmp_path)
    runner = HandlerHarnessProcessRunner(environment=Environment(tmp_path), agents={})
    got = await runner.run(declared("harness_opus"), "p", 20)
    assert got.model == "claude-haiku-5-5"  # not the requested opus


@pytest.mark.parametrize("variable", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
async def test_api_key_login_is_refused_by_name_and_never_launched(
    tmp_path: Path, variable: str
) -> None:
    seen = fake_claude(tmp_path)
    runner = HandlerHarnessProcessRunner(
        environment=Environment(tmp_path, {variable: "sk-not-a-real-value"}),
        agents={},
    )
    got = await runner.run(declared("harness_sonnet"), "p", 20)
    assert got.exit_cause == "credential_refused"
    assert variable in got.detail
    assert "sk-not-a-real-value" not in got.model_dump_json()
    assert not seen.exists()


async def test_call_result_and_attempt_carry_the_answering_model(
    tmp_path: Path,
) -> None:
    fake_claude(tmp_path)
    sink = ListCostSink()
    runner = HandlerHarnessProcessRunner(environment=Environment(tmp_path), agents={})
    call = HandlerHarnessBackendCall(
        runner=runner, gate=AcceptAllGate(), cost_sink=sink, overlay=load_overlay()
    )
    result = await call.handle(
        call_request("document", pinned_backend_id="harness-claude-opus")
    )
    assert result.ok
    assert result.model_name == "claude-haiku-5-5"
    assert sink.records[0].model_name == "claude-haiku-5-5"
