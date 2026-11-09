# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The agent-prompt gh-read guard: refuses a prompt that tells a subagent to call gh for a read the mirror serves,
admits the same prompt written with the mirror verb (OMN-23307). Also runs the process entry the hook calls."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.enums import (
    EnumAgentPromptGhReadVerdict,
    EnumGuardSessionMode,
)
from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.handlers.handler_agent_prompt_gh_read_guard import (
    HandlerAgentPromptGhReadGuard,
)
from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.handlers.handler_contract_policy_source import (
    GuardPolicyError,
    load_policy,
)
from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.models import (
    ModelAgentPromptGhReadDecision,
    ModelAgentPromptGhReadRequest,
    ModelAgentPromptSurface,
)
from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.runtime_agent_prompt_gh_read import (
    build_request,
    main,
)
from omnimarket.nodes.node_lane_gh_read_scan_compute.handlers.handler_gh_read_scan import (
    HandlerGhReadScan,
)
from omnimarket.nodes.node_lane_gh_read_scan_compute.models.model_gh_read_scan import (
    ModelGhReadScanRequest,
    ModelGhReadScanResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_agent_prompt_gh_read_guard_effect"
)
REFUSED_PROMPT = (
    "Check the pull request.\n"
    "Run `gh pr view 123 --json statusCheckRollup` and report the checks."
)
MIRROR_PROMPT = (
    "Check the pull request.\n"
    "Run `onex scm pr-checks OmniNode-ai/omnimarket#123` and report the checks."
)


def agent_payload(prompt: str) -> dict[str, object]:
    return {
        "tool_name": "Agent",
        "tool_input": {"prompt": prompt, "description": "check", "model": "sonnet"},
    }


def decide(
    payload: dict[str, object], mode: EnumGuardSessionMode = EnumGuardSessionMode.FULL
) -> ModelAgentPromptGhReadDecision:
    request = build_request(payload, load_policy({}), mode)
    return HandlerAgentPromptGhReadGuard().handle(request)


def test_agent_prompt_gh_read_refused_names_the_mirror_verb() -> None:
    decision = decide(agent_payload(REFUSED_PROMPT))
    assert decision.verdict is EnumAgentPromptGhReadVerdict.REFUSE
    assert decision.refusal_class == "agent-prompt-gh-read"
    assert [f.command for f in decision.findings] == ["gh pr view"]
    assert decision.findings[0].path == "Agent prompt"
    assert decision.findings[0].line == 2
    assert "onex scm pr-view" in decision.reason
    assert decision.hook_payload()["decision"] == "block"


def test_agent_prompt_gh_read_admits_the_same_prompt_using_the_mirror_verb() -> None:
    decision = decide(agent_payload(MIRROR_PROMPT))
    assert decision.verdict is EnumAgentPromptGhReadVerdict.ALLOW
    assert decision.findings == ()


@pytest.mark.parametrize(
    "line",
    [
        "Run gh run view 99 --log and read the failure.",
        "Fetch gh api repos/OmniNode-ai/omnimarket/pulls/5 for the head.",
        "Read https://api.github.com/repos/OmniNode-ai/omnimarket/pulls/5 for the head.",
        "Then gh pr list --search OMN-1 for the PRs.",
        "Open it with gh pr create --title t --body b.",
    ],
)
def test_agent_prompt_gh_read_refuses_each_gh_shape_the_mirror_serves(
    line: str,
) -> None:
    assert decide(agent_payload(line)).verdict is EnumAgentPromptGhReadVerdict.REFUSE


@pytest.mark.parametrize(
    "line",
    [
        "Never run gh pr view; read it with onex scm pr-view instead.",
        "gh run view is refused from a lane, so use onex scm run-view.",
        "Use onex scm pr-checks o/r#9 instead of gh pr checks.",
        "Read the state with pr_state_local.py --pr o/r#9.",
    ],
)
def test_agent_prompt_gh_read_admits_a_prompt_that_names_gh_only_to_forbid_it(
    line: str,
) -> None:
    assert decide(agent_payload(line)).verdict is EnumAgentPromptGhReadVerdict.ALLOW


def test_agent_prompt_gh_read_judges_every_finding_of_a_long_prompt() -> None:
    prompt = (
        "Step 1: gh pr view 1.\n"
        "Step 2: onex scm pr-checks o/r#1.\n"
        "Step 3: gh run view 7 --log."
    )
    assert [f.line for f in decide(agent_payload(prompt)).findings] == [1, 3]


def test_agent_prompt_gh_read_lite_mode_refuses_nothing() -> None:
    decision = decide(agent_payload(REFUSED_PROMPT), EnumGuardSessionMode.LITE)
    assert decision.verdict is EnumAgentPromptGhReadVerdict.ALLOW


def test_agent_prompt_gh_read_other_tool_is_not_judged() -> None:
    payload = {"tool_name": "Read", "tool_input": {"prompt": REFUSED_PROMPT}}
    assert decide(payload).verdict is EnumAgentPromptGhReadVerdict.ALLOW


def test_agent_prompt_gh_read_workflow_script_file_is_judged_and_its_comments_are_not(
    tmp_path: Path,
) -> None:
    script = tmp_path / "wf.js"
    script.write_text(
        "// gh pr view 5 is refused here\n"
        "await agent('Run gh run view 9 --log', {model: 'sonnet'})\n"
    )
    decision = decide(
        {"tool_name": "Workflow", "tool_input": {"scriptPath": str(script)}}
    )
    assert decision.verdict is EnumAgentPromptGhReadVerdict.REFUSE
    assert [(f.path, f.line, f.command) for f in decision.findings] == [
        ("Workflow scriptPath", 2, "gh run view")
    ]


def test_agent_prompt_gh_read_workflow_unreadable_script_is_the_model_guards_to_refuse(
    tmp_path: Path,
) -> None:
    payload = {
        "tool_name": "Workflow",
        "tool_input": {"scriptPath": str(tmp_path / "missing.js")},
    }
    assert decide(payload).verdict is EnumAgentPromptGhReadVerdict.ALLOW


def test_agent_prompt_gh_read_workflow_inline_script_is_judged() -> None:
    payload = {
        "tool_name": "Workflow",
        "tool_input": {"script": "await agent('gh pr checks 4', {})"},
    }
    assert decide(payload).verdict is EnumAgentPromptGhReadVerdict.REFUSE


def test_agent_prompt_gh_read_overlay_adds_a_judged_field_and_never_removes_one(
    tmp_path: Path,
) -> None:
    node_dir = tmp_path / "node_agent_prompt_gh_read_guard_effect"
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(
            {"judged_fields": {"Agent": ["notes"], "SendMessage": ["message"]}}
        )
    )
    policy = load_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert policy.judged_fields["Agent"] == ("prompt", "description", "notes")
    assert policy.judged_fields["SendMessage"] == ("message",)
    payload = {
        "tool_name": "SendMessage",
        "tool_input": {"message": "run gh pr view 3"},
    }
    decision = HandlerAgentPromptGhReadGuard().handle(build_request(payload, policy))
    assert decision.verdict is EnumAgentPromptGhReadVerdict.REFUSE


def test_agent_prompt_gh_read_unreadable_policy_cannot_judge(tmp_path: Path) -> None:
    bad = tmp_path / "contract.yaml"
    bad.write_text("name: x\n")
    with pytest.raises(GuardPolicyError):
        load_policy({}, contract=bad)


def test_agent_prompt_gh_read_contract_declares_the_bus_topics_and_policy() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert contract["guard_policy"]["judged_fields"]["Agent"] == [
        "prompt",
        "description",
    ]


def run_entry(payload: object, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(NODE_DIR.parents[2])}
    env.pop("ONEX_SKILL_OVERLAY_ROOTS", None)
    module = "omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.runtime_agent_prompt_gh_read"
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_agent_prompt_gh_read_process_entry_exit_codes() -> None:
    refused = run_entry(agent_payload(REFUSED_PROMPT))
    assert refused.returncode == 2
    body = json.loads(refused.stdout)
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "onex scm pr-view" in body["reason"]
    admitted = run_entry(agent_payload(MIRROR_PROMPT))
    assert (admitted.returncode, admitted.stdout) == (0, "")
    assert run_entry(agent_payload(REFUSED_PROMPT), "--mode", "lite").returncode == 0
    assert run_entry("not json").returncode == 1
    assert run_entry("[1]").returncode == 1


def test_agent_prompt_gh_read_main_returns_the_exit_code_in_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps(agent_payload(REFUSED_PROMPT)))
    )
    assert main([]) == 2
    assert "gh pr view" in capsys.readouterr().out


def test_agent_prompt_gh_read_a_blind_scanner_turns_the_refusal_assertion_red() -> None:
    class Blind(HandlerGhReadScan):
        def handle(self, request: ModelGhReadScanRequest) -> ModelGhReadScanResult:
            return ModelGhReadScanResult(findings=())

    request = ModelAgentPromptGhReadRequest(
        tool_name="Agent",
        surfaces=(ModelAgentPromptSurface(label="Agent prompt", text=REFUSED_PROMPT),),
    )
    real = HandlerAgentPromptGhReadGuard().handle(request)
    blind = HandlerAgentPromptGhReadGuard(scanner=Blind()).handle(request)
    assert real.verdict is EnumAgentPromptGhReadVerdict.REFUSE
    assert blind.verdict is EnumAgentPromptGhReadVerdict.ALLOW
