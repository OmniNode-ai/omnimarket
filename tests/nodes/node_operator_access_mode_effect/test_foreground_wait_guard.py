"""Each command-shape refusal and the replacement it names, through the hook entry (OMN-20893).

RULING 2026-10-12T05:03:28Z lane=orchestrator-1d5b: for each enforcement gate added since
2026-10-08, check whether its replacement path works end to end, and fall back to the old path
only where it does not. Lane omn20893-46cf drove every refusal kind of the supervision hooks
(tooling#402) and the single-command form lanes use instead (RULING 2026-10-11T13:14:12Z
lane=orchestrator-9f8a) through the real entry point, and ran each replacement for real: every one
worked, so every gate stays strict. These tests keep it that way. Each case is a pair, the refused
form and the form that does the same job, and both go through ``python -m <entry> pre-tool`` or
``stop`` exactly as the registered hook runs them, with only the session's environment:

* a main-session Bash call that waits (sleep, a --watch flag, a long timeout) is refused in local
  and remote mode alike and names the background Workflow; inside a Workflow agent it passes;
* a chained command is refused in an operator-facing and a headless main session, and the one
  plain command that does the same job passes: an environment variable for one tool, a file read,
  a loop over items, a commit message without a heredoc, a working directory without ``cd``;
* ssh is refused in the main session and the refusal names /omni:lab-run, whose call passes;
* a reply with a bare ticket or PR id is refused once and passes when the id is named.

The ticket's falsifiers select by name: ``-k refuses_both_modes``, ``-k background_allowed`` and
``-k mode_independent``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_operator_access_mode_effect.handlers import (
    handler_operator_hooks_installer as installer_module,
)

ENTRY_MODULE = installer_module.ENTRY_MODULE
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FACING = ("local_cli", "remote_bridge")
SESSIONS = (*FACING, "headless_sdk")


def _session(name: str, state: Path) -> tuple[dict[str, str], dict[str, Any]]:
    loaded = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    environ = dict(loaded["environ"])
    environ["OMNI_ACCESS_MODE_STATE_DIR"] = str(state)
    return environ, dict(loaded["payload"])


def _hook(
    event: str, payload: dict[str, Any], environ: dict[str, str]
) -> tuple[int, str]:
    """Run the hook entry as the registered hook runs it; return (exit code, what it said)."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent", **environ}
    result = subprocess.run(
        [sys.executable, "-B", "-m", ENTRY_MODULE, event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    return result.returncode, (result.stderr + result.stdout).strip()


def _bash(
    name: str,
    state: Path,
    command: str,
    *,
    timeout_ms: int | None = None,
    agent_id: str | None = None,
    permission_mode: str | None = None,
) -> tuple[int, str]:
    environ, payload = _session(name, state)
    if permission_mode is not None:
        payload["permission_mode"] = permission_mode
    tool_input: dict[str, Any] = {"command": command}
    if timeout_ms is not None:
        tool_input["timeout"] = timeout_ms
    payload = {**payload, "hook_event_name": "PreToolUse", "tool_name": "Bash"}
    payload["tool_input"] = tool_input
    if agent_id:
        payload["agent_id"] = agent_id
    return _hook("pre-tool", payload, environ)


# --- AC1 to AC3: waits ------------------------------------------------------------------------

WAITS = [
    pytest.param("sleep 300", None, id="sleep"),
    pytest.param(
        "gh pr checks 4802 --repo OmniNode-ai/omnibase_infra --watch", None, id="watch"
    ),
    pytest.param("timeout 600 python3 poll.py", None, id="long-timeout"),
    pytest.param("python3 poll.py", 600_000, id="long-bash-timeout"),
    pytest.param("tail -f /tmp/lane.log", None, id="tail-follow"),
]


@pytest.mark.parametrize(("command", "timeout_ms"), WAITS)
def test_a_wait_refuses_both_modes_and_names_the_workflow(
    command: str, timeout_ms: int | None, tmp_path: Path
) -> None:
    for name in FACING:
        code, said = _bash(name, tmp_path, command, timeout_ms=timeout_ms)
        assert code == 2, (name, command, said)
        assert "background Workflow" in said, said


@pytest.mark.parametrize(("command", "timeout_ms"), WAITS)
def test_a_wait_is_background_allowed_inside_a_workflow_agent(
    command: str, timeout_ms: int | None, tmp_path: Path
) -> None:
    for name in SESSIONS:
        code, said = _bash(
            name, tmp_path, command, timeout_ms=timeout_ms, agent_id="wf-agent-1"
        )
        assert code == 0, (name, command, said)
        assert said == ""


@pytest.mark.parametrize(("command", "timeout_ms"), WAITS)
def test_the_wait_refusal_is_mode_independent(
    command: str, timeout_ms: int | None, tmp_path: Path
) -> None:
    said = {
        name: _bash(name, tmp_path, command, timeout_ms=timeout_ms)[1]
        for name in FACING
    }
    assert said["local_cli"] == said["remote_bridge"]


# --- replacement forms: each refused form and the one plain command that does its job ---------

REPLACEMENTS = [
    pytest.param(
        "export PROBE=1; python3 tool.py",
        "PROBE=1 python3 tool.py",
        "chained commands",
        id="env-var-for-a-tool",
    ),
    pytest.param(
        "cd /repo && env -u PYTHONPATH uv run --frozen python -m pkg.node",
        "env -u PYTHONPATH uv run --directory /repo --frozen python -m pkg.node",
        "chained commands",
        id="tool-in-a-directory",
    ),
    pytest.param(
        "cd /repo && cat a.txt", "cat /repo/a.txt", "chained commands", id="read-a-file"
    ),
    pytest.param(
        "for x in one two; do echo item-$x; done",
        "printf '%s\\n' one two | xargs -I{} echo item-{}",
        "chained commands",
        id="loop-over-items",
    ),
    pytest.param(
        "for x in one two\ndo\n  echo $x\ndone",
        'python3 -c \'for x in ("one", "two"):\n    print(x)\'',
        "chained commands",
        id="multi-line-loop",
    ),
    pytest.param(
        "git -C /repo commit -F - <<'EOF'\nsubject\n\nbody\nEOF",
        "git -C /repo commit -F /scratch/commit-msg.txt",
        "chained commands",
        id="heredoc-commit-message",
    ),
    pytest.param(
        "cat > /scratch/body.md <<'EOF'\nbody\nEOF",
        'git -C /repo commit -m "subject\n\nbody in quotes"',
        "chained commands",
        id="heredoc-to-quoted-message",
    ),
    pytest.param(
        'L="$ONEX_LEDGER_PATH"\npython3 onex_ledger.py "$L" --append "row"',
        'python3 onex_ledger.py "$ONEX_LEDGER_PATH" --append "$(date -u +%FT%TZ) | CLAIM | row"',
        "chained commands",
        id="ledger-template-variable",
    ),
    pytest.param(
        "(cd /repo && git log --oneline -1)",
        "git -C /repo log --oneline -1",
        "chained commands",
        id="subshell-with-cd",
    ),
    pytest.param(
        "cd /repo", "git -C /repo rev-parse --show-toplevel", "git -C", id="cd"
    ),
    pytest.param(
        "ssh h201 hostname",
        "python3 onex_lab_run.py --via ssh --lane l --repo r --ref main --host h201 -- hostname",
        "/omni:lab-run",
        id="ssh-lab-probe",
    ),
]


@pytest.mark.parametrize(("refused", "replacement", "named"), REPLACEMENTS)
def test_the_refused_form_is_refused_and_its_replacement_passes(
    refused: str, replacement: str, named: str, tmp_path: Path
) -> None:
    for name in SESSIONS:
        # A chain is refused where a permission prompt could stop it; the recorded local and
        # remote sessions run in bypassPermissions, where it passes (RULING 2026-10-13T04:14:36Z).
        code, said = _bash(name, tmp_path, refused, permission_mode="default")
        assert code == 2, (name, refused, said)
        assert said.startswith("BLOCKED (foreground Bash shape)"), said
        assert named in said, said
        code, said = _bash(name, tmp_path, replacement)
        assert code == 0, (name, replacement, said)


def test_a_chain_passes_where_nothing_can_prompt_and_cd_ssh_still_refuse(
    tmp_path: Path,
) -> None:
    """The same hook entry, bypassPermissions: chains pass, ``cd`` and ``ssh`` in a chain do not."""
    for name in SESSIONS:
        chain = "export PROBE=1; python3 tool.py"
        assert _bash(name, tmp_path, chain, permission_mode="bypassPermissions")[0] == 0
        for refused in (
            "cd /repo && cat a.txt",
            "ls /tmp; ssh h201 hostname",
            "ls; sleep 5",
        ):
            code, said = _bash(
                name, tmp_path, refused, permission_mode="bypassPermissions"
            )
            assert code == 2, (name, refused, said)
        code, said = _bash(
            name,
            tmp_path,
            "ls /tmp; tail -f /tmp/x",
            permission_mode="bypassPermissions",
        )
        assert (code == 2) == (name in FACING), (name, said)


def test_a_headless_run_polls_inside_one_bounded_command(tmp_path: Path) -> None:
    """A headless run has no wait rule: a bounded poll passes there, and only there."""
    poll = "timeout 300 bash -c 'until test -e /tmp/ready; do sleep 5; done'"
    assert _bash("headless_sdk", tmp_path, poll)[0] == 0
    for name in FACING:
        assert _bash(name, tmp_path, poll)[0] == 2


# --- the reply check: a bare id is refused once and passes when named -------------------------

REPLIES = [
    pytest.param(
        "Done: tooling#402 is merged.",
        "Done: the supervision hooks (tooling#402) are merged.",
        id="bare-pr",
    ),
    pytest.param(
        "| tooling#483 | abc1234 |",
        "| tooling#483 | gate replacement tests | abc1234 |",
        id="table-row",
    ),
]


@pytest.mark.parametrize(("bare", "named"), REPLIES)
def test_a_bare_id_refuses_both_modes_and_passes_once_named(
    bare: str, named: str, tmp_path: Path
) -> None:
    for name in FACING:
        environ, payload = _session(name, tmp_path)
        payload = {**payload, "hook_event_name": "Stop"}
        code, said = _hook("stop", {**payload, "last_assistant_message": bare}, environ)
        assert code == 0
        assert json.loads(said)["decision"] == "block", said
        code, said = _hook(
            "stop", {**payload, "last_assistant_message": named}, environ
        )
        assert (code, said) == (0, ""), said
