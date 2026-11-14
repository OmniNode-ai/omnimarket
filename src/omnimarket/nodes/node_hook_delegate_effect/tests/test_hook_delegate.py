# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_hook_delegate_effect: a delegated subagent task comes back answered, or passes through.

Failure modes, each caught below (found 2026-11-13 driving the real hook):
  N1  a delegatable task is blocked with a routing label and no answer.
  N2  the classifier's intent is not passed on, so onex delegate guesses the task
      class: a prose question judged as code_generation was vetoed for not
      compiling as Python.
  N3  no onex, a failed run or a run past the limit still blocks the subagent.
  N4  a plain line printed after the JSON document hides the answer.
  N5  a blank, missing or malformed answer is reported as answered.
  N6  the ONEX gateway already routes the session and the task is delegated twice.
  N7  a missing or bad time limit is accepted.
  N8  the real process adapter raises instead of reporting a missing onex or a timeout.
  N9  the hook runs whichever `onex` is first on Claude Code's PATH (seen
      2026-11-13: a launcher whose older uv-tool onex knew no Reflection AI)
      instead of the one installed beside the hook's own Python.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_hook_delegate_effect.adapters.adapter_delegate_cli import (
    AdapterDelegateCli,
)
from omnimarket.nodes.node_hook_delegate_effect.container import build_handler
from omnimarket.nodes.node_hook_delegate_effect.models import (
    EnumDelegateCliState,
    EnumHookDelegateOutcome,
    ModelDelegateCliRun,
    ModelHookDelegateRequest,
)

pytestmark = pytest.mark.unit

_ANSWER = "A list comprehension builds a list from an iterable in one expression."
_DOC = {
    "run_id": "29b15069-0000",
    "result": {"response": _ANSWER, "model_name": "Qwen3.8-27B"},
}


class _Cli:
    """A scripted `onex`: records each call and answers what it was given."""

    def __init__(self, run: ModelDelegateCliRun) -> None:
        self.ran = run
        self.calls: list[tuple[list[str], float]] = []

    def run(self, args: list[str], timeout_seconds: float) -> ModelDelegateCliRun:
        self.calls.append((args, timeout_seconds))
        return self.ran


def _exited(stdout: str, code: int = 0) -> ModelDelegateCliRun:
    return ModelDelegateCliRun(
        state=EnumDelegateCliState.EXITED, exit_code=code, stdout=stdout
    )


def _request(intent: str = "research") -> ModelHookDelegateRequest:
    return ModelHookDelegateRequest(task="explain", intent=intent, timeout_seconds=30)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_an_answer_comes_back_with_its_model_and_run() -> None:
    """N1, N2."""
    cli = _Cli(_exited(json.dumps(_DOC)))
    result = build_handler(cli).handle(_request())
    assert result.answered
    assert (result.answer, result.model, result.run_id) == (
        _ANSWER,
        "Qwen3.8-27B",
        "29b15069-0000",
    )
    args, limit = cli.calls[0]
    assert args[:3] == ["delegate", "explain", "--json"]
    assert args[args.index("--task-type") + 1] == "research"
    assert limit == 30


def test_an_intent_with_no_public_task_class_is_left_to_onex_delegate() -> None:
    """N2: debug has no public class; naming a wrong one would be worse."""
    cli = _Cli(_exited(json.dumps(_DOC)))
    build_handler(cli).handle(_request("debug"))
    assert "--task-type" not in cli.calls[0][0]


@pytest.mark.parametrize(
    ("ran", "outcome"),
    [
        (ModelDelegateCliRun(state=EnumDelegateCliState.NOT_FOUND), "no_onex"),
        (ModelDelegateCliRun(state=EnumDelegateCliState.TIMED_OUT), "timed_out"),
        (ModelDelegateCliRun(state=EnumDelegateCliState.NOT_STARTED), "failed"),
        (_exited(json.dumps(_DOC), code=1), "failed"),
    ],
)
def test_a_run_that_did_not_answer_passes_the_task_through(
    ran: ModelDelegateCliRun, outcome: str
) -> None:
    """N3: a failed run that still printed an answer-shaped document is not trusted."""
    result = build_handler(_Cli(ran)).handle(_request())
    assert not result.answered
    assert result.outcome == EnumHookDelegateOutcome(outcome)


def test_a_line_after_the_json_does_not_hide_the_answer() -> None:
    """N4."""
    result = build_handler(_Cli(_exited(json.dumps(_DOC) + "\nreceipt written\n")))
    assert result.handle(_request()).answer == _ANSWER


def test_the_real_cli_receipt_envelope_returns_the_answer() -> None:
    """The installed CLI nests its result inside receipt, not at the top level."""
    doc = {
        "run_id": _DOC["run_id"],
        "status": "success",
        "receipt": {
            "status": "success",
            "result": {
                "response": _ANSWER,
                "model_name": "Qwen3.8-27B",
                "status": "completed",
                "quality_gate_passed": True,
            },
        },
    }
    result = build_handler(_Cli(_exited(json.dumps(doc)))).handle(_request())
    assert result.answered
    assert result.answer == _ANSWER
    assert result.run_id == _DOC["run_id"]


def test_a_failed_receipt_with_answer_text_is_not_accepted() -> None:
    doc = {**_DOC, "status": "failed"}
    result = build_handler(_Cli(_exited(json.dumps(doc)))).handle(_request())
    assert not result.answered


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json at all\n",
        json.dumps({"run_id": "r", "result": {"response": "   "}}),
        json.dumps({"run_id": "r", "result": {"model_name": "m"}}),
        json.dumps({"run_id": "r", "result": "text"}),
        json.dumps(["a", "list"]),
    ],
)
def test_no_usable_answer_is_never_reported_as_answered(stdout: str) -> None:
    """N5."""
    result = build_handler(_Cli(_exited(stdout))).handle(_request())
    assert result.outcome is EnumHookDelegateOutcome.NO_ANSWER


@pytest.mark.parametrize("journal", ["enabled", "unreadable"])
def test_a_session_the_gateway_routes_is_not_delegated_again(
    home: Path, journal: str
) -> None:
    """N6: an unreadable journal counts as enabled."""
    path = home / ".onex" / "claude_gateway.json"
    path.parent.mkdir()
    if journal == "enabled":
        path.write_text('{"schema_version":1,"phase":"enabled"}\n')
    else:
        path.mkdir()  # reading a directory raises an OSError
    cli = _Cli(_exited(json.dumps(_DOC)))
    result = build_handler(cli).handle(_request())
    assert result.outcome is EnumHookDelegateOutcome.GATEWAY_OWNS_SESSION
    assert cli.calls == []


def test_a_disabled_gateway_delegates(home: Path) -> None:
    """N6, the other side."""
    path = home / ".onex" / "claude_gateway.json"
    path.parent.mkdir()
    path.write_text('{"schema_version":1,"phase":"disabled"}\n')
    assert build_handler(_Cli(_exited(json.dumps(_DOC)))).handle(_request()).answered


def test_the_request_takes_the_prompt_else_the_whole_input() -> None:
    """The subagent's prompt is the task; without one, the tool input as text."""
    asked = ModelHookDelegateRequest.of({"prompt": "p"}, "{...}", "research", "240")
    assert (asked.task, asked.intent, asked.timeout_seconds) == ("p", "research", 240.0)
    blank = ModelHookDelegateRequest.of({"prompt": "  "}, "{...}", None, 5)
    assert (blank.task, blank.intent) == ("{...}", "")


@pytest.mark.parametrize("limit", ["", "soon", "0", "-5", None])
def test_a_missing_or_bad_limit_is_refused(limit: object) -> None:
    """N7: the hook passes the task through when the request cannot be built."""
    with pytest.raises(ValidationError):
        ModelHookDelegateRequest.of({"prompt": "p"}, "", "research", limit)


def test_the_real_adapter_reports_a_missing_onex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N8."""
    ran = AdapterDelegateCli(onex=tmp_path / "no-onex").run(
        ["delegate", "x", "--json"], 5
    )
    assert ran.state is EnumDelegateCliState.NOT_FOUND


@pytest.mark.parametrize(
    ("body", "limit", "state", "stdout"),
    [
        ('echo "$@"', 5, EnumDelegateCliState.EXITED, "delegate x --json\n"),
        ("sleep 5", 0.5, EnumDelegateCliState.TIMED_OUT, ""),
    ],
)
def test_the_real_adapter_runs_onex_and_honours_the_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    limit: float,
    state: EnumDelegateCliState,
    stdout: str,
) -> None:
    """N8: a real process; the limit ends it."""
    onex = tmp_path / "onex"
    onex.write_text(f"#!/bin/sh\n{body}\n")
    onex.chmod(0o755)
    monkeypatch.setenv("PATH", f"/bin{os.pathsep}/usr/bin")  # sleep and echo
    ran = AdapterDelegateCli(onex=onex).run(["delegate", "x", "--json"], limit)
    assert (ran.state, ran.stdout) == (state, stdout)


def test_the_onex_beside_this_python_is_used_before_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N9."""
    import sys

    from omnimarket.nodes.node_hook_delegate_effect.adapters import adapter_delegate_cli

    other = tmp_path / "onex"
    other.write_text("#!/bin/sh\necho other\n")
    other.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    beside = Path(sys.executable).parent / "onex"
    assert beside.is_file()  # this venv's own onex
    assert adapter_delegate_cli.installed_onex() == beside
