# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude credential probe: one bounded call, a typed redacted receipt, no credential field (OMN-21626).

The executable is a fixture script, so the tests hold the process boundary: the prompt arrives on stdin, the
credential only in the environment, scrubbed variables never reach the child, and every ``sk-ant-`` string is
redacted from the excerpt.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_llm_delegation_call_effect import (
    HandlerClaudeCredentialProbe,
    ModelClaudeCredentialProbeReceipt,
    ModelClaudeCredentialProbeRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    runtime_claude_probe as runtime,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_claude_credential_probe import (
    ModelClaudeCredentialProbePolicy,
    load_claude_credential_probe_policy,
)
from tests.overlay_bound_support import durable_overlay

pytestmark = pytest.mark.unit


def engine(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "engine"
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o700)
    return path


def policy(
    binary: Path, *, output_chars: int = 600
) -> ModelClaudeCredentialProbePolicy:
    return ModelClaudeCredentialProbePolicy(
        executable=str(binary),
        argv=("-p", "--output-format", "text"),
        scrubbed_env=("ANTHROPIC_API_KEY", "CLAUDECODE"),
        max_timeout_seconds=30,
        terminate_grace_seconds=1,
        output_chars=output_chars,
    )


def request(timeout: float = 5) -> ModelClaudeCredentialProbeRequest:
    return ModelClaudeCredentialProbeRequest(
        prompt="say ok", model="fixture-model", timeout_seconds=timeout
    )


def test_the_prompt_is_on_stdin_the_credential_in_the_environment_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = "sk-ant-" + "oat01-" + "x" * 40
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-the-child")
    binary = engine(
        tmp_path,
        """import json, os, sys
print(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read(),
  "key": os.environ.get("ANTHROPIC_API_KEY"), "token": os.environ["CLAUDE_CODE_OAUTH_TOKEN"]}))
""",
    )
    receipt = HandlerClaudeCredentialProbe(policy(binary)).handle(request())
    assert receipt.exit_cause == "answered"
    assert receipt.exit_code == 0
    seen = json.loads(receipt.output)
    assert seen["argv"] == ["-p", "--output-format", "text", "--model", "fixture-model"]
    assert seen["stdin"] == "say ok"
    assert seen["key"] is None
    assert seen["token"] == "<redacted>"  # the child had it; the receipt shows no value
    assert fake not in receipt.model_dump_json()


def test_a_refusal_keeps_its_text_and_the_exit_code(tmp_path: Path) -> None:
    binary = engine(
        tmp_path,
        'import sys\nprint("Your organization has disabled Claude", file=sys.stderr)\nsys.exit(1)\n',
    )
    receipt = HandlerClaudeCredentialProbe(policy(binary)).handle(request())
    assert receipt.exit_cause == "process_failed"
    assert receipt.exit_code == 1
    assert "organization has disabled" in receipt.output


def test_the_excerpt_is_bounded_and_redacted(tmp_path: Path) -> None:
    binary = engine(tmp_path, 'print("sk-ant-oat01-" + "z" * 80 + " " + "w" * 900)\n')
    receipt = HandlerClaudeCredentialProbe(policy(binary, output_chars=50)).handle(
        request()
    )
    assert len(receipt.output) == 50
    assert "sk-ant" not in receipt.output


def test_a_slow_call_times_out_and_a_missing_binary_is_a_launch_failure(
    tmp_path: Path,
) -> None:
    slow = engine(tmp_path, "import time\ntime.sleep(30)\n")
    assert (
        HandlerClaudeCredentialProbe(policy(slow)).handle(request(timeout=1)).exit_cause
        == "timeout"
    )
    gone = tmp_path / "absent"
    assert (
        HandlerClaudeCredentialProbe(policy(gone)).handle(request()).exit_cause
        == "launch_failed"
    )


def test_a_deadline_over_the_contract_ceiling_is_refused(tmp_path: Path) -> None:
    binary = engine(tmp_path, "print('ok')\n")
    with pytest.raises(ValueError, match="exceeds contract policy"):
        HandlerClaudeCredentialProbe(policy(binary)).handle(request(timeout=31))


def test_the_contract_policy_loads_and_no_model_has_a_credential_field() -> None:
    packaged = load_claude_credential_probe_policy()
    assert packaged.executable == "claude"
    assert "ANTHROPIC_API_KEY" in packaged.scrubbed_env
    fields = set(ModelClaudeCredentialProbeRequest.model_fields) | set(
        ModelClaudeCredentialProbeReceipt.model_fields
    )
    assert not {f for f in fields if "token" in f or "secret" in f or "key" in f}
    with pytest.raises(ValidationError):
        ModelClaudeCredentialProbeRequest.model_validate(
            {"prompt": "p", "model": "m", "timeout_seconds": 1, "token": "x"}
        )


def test_the_runtime_reads_one_request_and_prints_one_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    binary = engine(tmp_path, "print('ok')\n")
    monkeypatch.setattr(
        runtime,
        "HandlerClaudeCredentialProbe",
        lambda: HandlerClaudeCredentialProbe(policy(binary)),
    )
    lane_overlay = tmp_path / "lane_overlay.json"
    lane_overlay.write_text(json.dumps(durable_overlay().model_dump(mode="json")))
    argv = ["--overlay", str(lane_overlay)]
    monkeypatch.setattr(sys, "stdin", io.StringIO(request().model_dump_json()))
    assert runtime.main(argv) == 0
    receipt = ModelClaudeCredentialProbeReceipt.model_validate_json(
        capsys.readouterr().out
    )
    assert receipt.output == "ok"
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert runtime.main(argv) == runtime.EXIT_INVALID
