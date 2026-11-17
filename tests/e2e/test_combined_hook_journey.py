# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install and execute both hook boundaries with isolated Claude configuration.

Failure modes: accepted prompt still calls Claude; declined, vetoed, failed or
timed-out delegation blocks Claude; skip/OFF calls a model; history is lost or
repeated; installer changes model/auth/provider or duplicates registrations.
The local fake CLI proves process wiring and controls, not real model inference.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.release_proof]

_TASK = "What is a Python list comprehension? Answer in one sentence."
_AGENT_TASK = "Write one sentence explaining what a Python list comprehension is."
_ANSWER = "A list comprehension builds a list from an iterable in one expression."


def test_installed_combined_switch_preserves_configuration_and_returns_answers(
    tmp_path: Path,
) -> None:
    repo = Path(
        os.environ.get(
            "ONEX_COMBINED_HOOK_SOURCE",
            str(Path(__file__).resolve().parents[2].parent / "omniclaude"),
        )
    )
    installer = repo / "scripts/install-delegation.sh"
    if not installer.is_file():
        message = "Set ONEX_COMBINED_HOOK_SOURCE to the customer bundle"
        if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") == "1":
            pytest.fail(message)
        pytest.skip(message)
    home, config = tmp_path / "home", tmp_path / "claude"
    home.mkdir()
    config.mkdir()
    settings = config / "settings.json"
    original = {
        "model": "opus",
        "env": {
            "ANTHROPIC_BASE_URL": "https://provider.example.test",
            "ANTHROPIC_AUTH_TOKEN": "fixture-auth-preserve",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "user-opus",
        },
        "permissions": {"deny": ["Bash(rm *)"]},
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "echo user-owned"}]}
            ]
        },
    }
    settings.write_text(json.dumps(original))
    # An isolated interpreter with the tested installation's dependencies and a
    # deterministic onex beside it. No executable in the working venv is changed.
    tools = tmp_path / "tools"
    binary = tools / "bin"
    binary.mkdir(parents=True)
    venv = Path(sys.prefix)
    shutil.copyfile(venv / "pyvenv.cfg", tools / "pyvenv.cfg")
    (tools / "lib").symlink_to(venv / "lib", target_is_directory=True)
    python = binary / "python"
    python.symlink_to(sys.executable)
    (binary / "claude").write_text("#!/bin/sh\necho '2.1.323 (Claude Code)'\n")
    (binary / "claude").chmod(0o755)
    calls = tmp_path / "calls"
    mode = tmp_path / "mode.json"
    mode.write_text(json.dumps({"mode": "answered"}))
    fake = binary / "onex"
    receipt = {
        "run_id": "3bf71d7b-aeec-410e-990f-b997313a8381",
        "status": "success",
        "result": {
            "status": "completed",
            "quality_gate_passed": True,
            "response": _ANSWER,
            "model_name": "selected-model",
        },
    }
    fake.write_text(
        f"#!{python}\nimport json,pathlib,sys,time\n"
        f"p=pathlib.Path({str(calls)!r}); p.write_text((p.read_text() if p.exists() else '')+json.dumps(sys.argv[1:])+'\\n')\n"
        f"mode=json.loads(pathlib.Path({str(mode)!r}).read_text())['mode']\n"
        "if mode == 'failed': sys.exit(1)\n"
        "if mode == 'timed_out': time.sleep(30)\n"
        f"doc={receipt!r}\n"
        "if mode == 'declined': doc['result']['response']='CANNOT_ANSWER'\n"
        "if mode == 'gate_failed': doc['result']['quality_gate_passed']=False\n"
        "print(json.dumps(doc))\n"
    )
    fake.chmod(0o755)
    env = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(config),
        "PATH": f"{binary}{os.pathsep}{os.environ['PATH']}",
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "OMNIBASE_ENV_FILE": str(home / ".omnibase/.env"),
        "ONEX_HOOKS_MASK": "0x800",
        "ONEX_HOOK_DELEGATE_TIMEOUT_S": "15",
        "ONEX_PROMPT_DELEGATE_TIMEOUT_S": "10",
        "TMPDIR": str(tmp_path),
    }
    install = [
        "bash",
        str(installer),
        "--apply",
        "--skip-pip",
        "--skip-smoke",
        "--python",
        str(python),
    ]
    proc = subprocess.run(install, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    once = json.loads(settings.read_text())
    assert {k: v for k, v in once.items() if k != "hooks"} == {
        k: v for k, v in original.items() if k != "hooks"
    }
    assert original["hooks"]["UserPromptSubmit"][0] in once["hooks"]["UserPromptSubmit"]
    proc = subprocess.run(install, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(settings.read_text()) == once

    def command(event: str) -> str:
        # The native mod invokes this installed boundary; it is no longer a
        # registered classic command. Actual Claude rendering is a separate
        # real-model release proof, not claimed by this fake-CLI process test.
        plugin = home / ".omninode/delegation/plugin/plugins/onex"
        runtime = json.loads((plugin / "hooks/delegation-runtime.json").read_text())
        return shlex.join(
            [
                "env",
                f"PLUGIN_PYTHON_BIN={runtime['python']}",
                str(plugin / "hooks/scripts/pre_tool_use_delegation.sh"),
            ]
        )

    def run(
        event: str, *, enabled: bool = True, prompt: str = _TASK, tool: str = "Agent"
    ) -> subprocess.CompletedProcess[str]:
        payload = {"hook_event_name": event, "session_id": "combined-journey"}
        if event == "UserPromptSubmit":
            payload["prompt"] = prompt
        else:
            payload.update(tool_name=tool, tool_input={"prompt": _AGENT_TASK})
        return subprocess.run(
            ["bash", "-c", command(event)],
            input=json.dumps(payload),
            env={**env, "ONEX_HOOKS_MASK": "0x800" if enabled else "0"},
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

    prompt = run("UserPromptSubmit")
    assert prompt.returncode == 0, prompt.stderr
    answered = json.loads(prompt.stdout)
    assert answered["decision"] == "block"
    assert _ANSWER in answered["reason"]
    assert "selected-model" in answered["reason"]
    assert receipt["run_id"] in answered["reason"]
    forwarded = json.loads(calls.read_text().splitlines()[0])
    assert "--system-prompt" in forwarded
    assert forwarded[forwarded.index("--criteria-mode") + 1] == "replace-task-class"
    assert forwarded[forwarded.index("--max-tokens") + 1] == "2048"

    def count() -> int:
        return len(calls.read_text().splitlines())

    assert count() == 1
    followup = run("UserPromptSubmit", prompt="@claude continue")
    assert followup.returncode == 0
    context = json.loads(followup.stdout)["hookSpecificOutput"]["additionalContext"]
    assert _ANSWER in context
    assert receipt["run_id"] in context
    assert not run("UserPromptSubmit", prompt="@claude continue").stdout.strip()
    assert not run("UserPromptSubmit", prompt="/help").stdout.strip()
    assert count() == 1

    outcomes = {}
    for case in ("declined", "gate_failed", "failed", "timed_out"):
        mode.write_text(json.dumps({"mode": case}))
        before = count()
        passed = run("UserPromptSubmit")
        assert passed.returncode == 0, passed.stderr
        assert not passed.stdout.strip(), (case, passed.stdout)
        assert count() == before + 1, case
        outcomes[case] = "pass_through"
    before = count()
    for event in ("UserPromptSubmit", "PreToolUse"):
        off = run(event, enabled=False)
        assert off.returncode == 0
        if event == "UserPromptSubmit":
            assert not off.stdout.strip()
        else:
            assert json.loads(off.stdout) == {
                "hook_event_name": event,
                "session_id": "combined-journey",
                "tool_name": "Agent",
                "tool_input": {"prompt": _AGENT_TASK},
            }
    assert count() == before
    mode.write_text(json.dumps({"mode": "answered"}))
    agent = run("PreToolUse")
    assert agent.returncode == 2, agent.stdout + agent.stderr
    assert _ANSWER in agent.stderr
    assert count() == before + 1
    assert run("PreToolUse", tool="Task").returncode == 2
    assert count() == before + 1  # exact task, same turn, same accepted receipt
    mode.write_text(json.dumps({"mode": "failed"}))
    failed = run("UserPromptSubmit")
    assert failed.returncode == 0
    assert not failed.stdout.strip()
    assert run("PreToolUse").returncode == 0
    assert json.loads(settings.read_text()) == once
    (tmp_path / "combined-hook-evidence.json").write_text(
        json.dumps(
            {
                "settings_preserved": True,
                "agent_task_reused_receipt": True,
                "history_delivered_once": True,
                "fallbacks": outcomes,
                "off_and_skip_make_no_calls": True,
                "prompt_exit": prompt.returncode,
                "prompt_decision": answered["decision"],
                "tool_exit": agent.returncode,
                "receipt_run_id": "3bf71d7b-aeec-410e-990f-b997313a8381",
                "inference": "fixture process; real inference requires separate proof",
            },
            indent=2,
        )
    )
