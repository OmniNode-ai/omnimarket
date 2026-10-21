"""The operator-supervision hooks write one refusal row per refusal (OMN-21020 AC1).

RULING 2026-10-12T05:03:28Z lane=orchestrator-1d5b and the standing rule that a gate is never
silent: the foreground Bash shape, foreground wait and operator-reply id guards (and the push gate
that shares their refusal path) refused on stderr or in a Stop block and left nothing a morning
sweep reads. Each refusal now calls the refusal recorder's shell entry, ``hook_refusal.sh`` in
omniclaude's hook library, with the gate's own guard name and reason class; the recorder resolves
the lane, redacts, dedupes and appends the row.

The installed hooks run from a copy of this node with the standard library alone, so they cannot
import omniclaude: they call the shell entry, which they find at
``ONEX_HOOK_REFUSAL_SEAM`` or ``$OMNI_HOME/omniclaude/plugins/onex/hooks/lib/hook_refusal.sh``.

This repo's test names a stand-in for that shell entry, spelled here, with the entry's contract
(``_OMNICLAUDE_HOOK_NAME``, ``_OMNICLAUDE_HOOK_PAYLOAD`` and ``hook_record_refusal <reason>
[detail]``) and appends the row it was asked for to a scratch ledger. The real entry is exercised
by omniclaude's own tests and by the lab run in the pull request, because a test that needs the
other clone would be skipped on a runner that lacks it.

Each case goes through ``python -m <entry> pre-tool`` or ``stop`` exactly as the registered hook
runs it. A refusing fixture yields exactly one row; an allowed fixture yields none.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_operator_access_mode_effect.handlers import (
    handler_operator_hooks_installer as installer_module,
)

pytestmark = pytest.mark.unit

ENTRY_MODULE = installer_module.ENTRY_MODULE
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FACING = ("local_cli", "remote_bridge")

#: The recorder's shell entry reduced to its contract: one function, the guard in
#: ``_OMNICLAUDE_HOOK_NAME``, the payload in ``_OMNICLAUDE_HOOK_PAYLOAD``. It appends the row the
#: recorder would, in the ledger's own cell grammar, to the scratch ledger.
FAKE_SEAM = """\
hook_record_refusal() {
    local cells="class=refusal | guard=$_OMNICLAUDE_HOOK_NAME | reason=${1:-unspecified}"
    cells="$cells | detail=${2:-} | payload=${_OMNICLAUDE_HOOK_PAYLOAD:-}"
    cells="$cells | pythonpath=${PYTHONPATH-unset}"
    printf '2026-10-12T00:00:00Z | FRICTION | lane=fixture | %s |\\n' "$cells" >> "$SCRATCH_LEDGER"
    return 0
}
"""

SHAPE_GUARD = "operator_access_mode_foreground_bash_shape"
WAIT_GUARD = "operator_access_mode_foreground_wait"
REPLY_GUARD = "operator_access_mode_reply_check"
PUSH_GUARD = "operator_access_mode_push_gate"


class Scratch:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.state = tmp_path / "state"
        self.ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
        self.ledger.write_text("# scratch ledger\n", encoding="utf-8")
        self.seam = tmp_path / "hook_refusal.sh"
        self.seam.write_text(FAKE_SEAM, encoding="utf-8")

    def hook(
        self,
        session: str,
        event: str,
        payload_update: dict[str, Any],
        *,
        with_seam: bool = True,
        environ_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        loaded = json.loads((FIXTURES / f"{session}.json").read_text(encoding="utf-8"))
        environ = dict(loaded["environ"])
        environ["OMNI_ACCESS_MODE_STATE_DIR"] = str(self.state)
        payload = {**loaded["payload"], **payload_update}
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/nonexistent",
            **environ,
            **(environ_extra or {}),
        }
        if with_seam:
            env["ONEX_HOOK_REFUSAL_SEAM"] = str(self.seam)
            env["SCRATCH_LEDGER"] = str(self.ledger)
        return subprocess.run(
            [sys.executable, "-B", "-m", ENTRY_MODULE, event],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )

    def bash(
        self,
        session: str,
        command: str,
        _environ: dict[str, str] | None = None,
        **extra: Any,
    ) -> subprocess.CompletedProcess[str]:
        return self.hook(
            session,
            "pre-tool",
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": command},
                **extra,
            },
            environ_extra=_environ,
        )

    def rows(self) -> list[str]:
        return [
            line
            for line in self.ledger.read_text(encoding="utf-8").splitlines()
            if "class=refusal" in line
        ]


def _cell(row: str, key: str) -> str:
    match = re.search(rf"\| {re.escape(key)}=([^|]*?) \|", row)
    assert match, f"{key}= missing from {row}"
    return match.group(1)


@pytest.fixture
def scratch(tmp_path: Path) -> Scratch:
    return Scratch(tmp_path)


@pytest.mark.parametrize("session", FACING)
def test_refusal_row_foreground_bash_shape_writes_one_row(
    scratch: Scratch, session: str
) -> None:
    result = scratch.bash(session, "cd /repo && ls")
    assert result.returncode == 2, result.stderr
    assert result.stderr.startswith("BLOCKED (foreground Bash shape): "), result.stderr
    (row,) = scratch.rows()
    assert _cell(row, "guard") == SHAPE_GUARD
    assert _cell(row, "reason") == "foreground-bash-shape"
    assert _cell(row, "detail").startswith("BLOCKED (foreground Bash shape): ")


@pytest.mark.parametrize("session", FACING)
def test_refusal_row_foreground_wait_writes_one_row(
    scratch: Scratch, session: str
) -> None:
    result = scratch.bash(session, "tail -f /tmp/lane.log")
    assert result.returncode == 2, result.stderr
    assert result.stderr.startswith("BLOCKED (foreground wait, OMN-20893): "), (
        result.stderr
    )
    (row,) = scratch.rows()
    assert _cell(row, "guard") == WAIT_GUARD
    assert _cell(row, "reason") == "foreground-wait"


def test_refusal_row_reply_ids_on_stop_writes_one_row(scratch: Scratch) -> None:
    result = scratch.hook(
        "local_cli",
        "stop",
        {
            "hook_event_name": "Stop",
            "last_assistant_message": "Done: tooling#402 is merged.",
        },
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block", result.stdout
    (row,) = scratch.rows()
    assert _cell(row, "guard") == REPLY_GUARD
    assert _cell(row, "reason") == "bare-ticket-or-pr-ids"


def test_refusal_row_reply_ids_on_send_writes_one_row(scratch: Scratch) -> None:
    result = scratch.hook(
        "local_cli",
        "pre-tool",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "SendUserMessage",
            "tool_input": {"message": "Done: tooling#402 is merged."},
        },
    )
    assert result.returncode == 2, result.stderr
    (row,) = scratch.rows()
    assert _cell(row, "guard") == REPLY_GUARD
    assert _cell(row, "reason") == "bare-ticket-or-pr-ids"


def test_refusal_row_reply_local_paths_names_its_own_reason(scratch: Scratch) -> None:
    result = scratch.hook(
        "remote_bridge",
        "stop",
        {
            "hook_event_name": "Stop",
            "last_assistant_message": "The report is at ~/report.md.",
        },
    )
    assert result.returncode == 0, result.stderr
    (row,) = scratch.rows()
    assert _cell(row, "reason") == "local-file-paths"


def test_refusal_row_push_gate_writes_one_row(scratch: Scratch) -> None:
    result = scratch.hook(
        "remote_bridge",
        "pre-tool",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "PushNotification",
            "tool_input": {"message": "status: all green", "status": "proactive"},
        },
    )
    assert result.returncode == 2, result.stderr
    (row,) = scratch.rows()
    assert _cell(row, "guard") == PUSH_GUARD
    assert _cell(row, "reason") == "not-an-attention-item"


def test_refusal_row_payload_names_the_lane_facts_and_not_the_command(
    scratch: Scratch,
) -> None:
    secret = "ghp_" + "S" * 36
    result = scratch.bash("local_cli", f"cd /repo && echo {secret}", agent_id=None)
    assert result.returncode == 2
    (row,) = scratch.rows()
    payload = json.loads(_cell(row, "payload"))
    assert payload["session_id"] == "33333333-3333-3333-3333-333333333333"
    assert "tool_input" not in payload
    assert secret not in row


def test_refusal_row_recorder_does_not_inherit_the_installed_copys_pythonpath(
    scratch: Scratch,
) -> None:
    """The installed command line sets PYTHONPATH to the stub copy of this node.

    Left in place it shadows the ledger writer's own package for the writer the recorder
    starts (ModuleNotFoundError: the ledger package), so the row is never written.
    Found on a lab host with the real recorder (OMN-21020).
    """
    stub = scratch.tmp / "stub-copy"
    stub.mkdir()
    environ = {"PYTHONPATH": str(stub)}
    result = scratch.bash("local_cli", "cd /repo && ls", _environ=environ)
    assert result.returncode == 2, result.stderr
    (row,) = scratch.rows()
    assert _cell(row, "pythonpath") == "unset"


def test_refusal_row_allowed_calls_write_none(scratch: Scratch) -> None:
    # A plain command, a wait inside a Workflow agent, a named id, and a headless run's reply.
    assert scratch.bash("local_cli", "ls /repo").returncode == 0
    assert (
        scratch.bash("remote_bridge", "sleep 300", agent_id="wf-agent-1").returncode
        == 0
    )
    named = scratch.hook(
        "local_cli",
        "stop",
        {
            "hook_event_name": "Stop",
            "last_assistant_message": (
                "Done: the supervision hooks (tooling#402) are merged."
            ),
        },
    )
    assert (named.returncode, named.stdout) == (0, "")
    assert scratch.rows() == []
    # Positive control: the same scratch ledger does take a row from a refusal.
    assert scratch.bash("local_cli", "cd /repo && ls").returncode == 2
    assert len(scratch.rows()) == 1


def test_refusal_row_unreachable_recorder_keeps_the_refusal(scratch: Scratch) -> None:
    """No seam named and no registry root: the refusal stands, no row, no crash."""
    result = scratch.hook(
        "local_cli",
        "pre-tool",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cd /repo && ls"},
        },
        with_seam=False,
    )
    assert result.returncode == 2, result.stderr
    assert result.stderr.startswith("BLOCKED (foreground Bash shape): "), result.stderr
    assert scratch.rows() == []
