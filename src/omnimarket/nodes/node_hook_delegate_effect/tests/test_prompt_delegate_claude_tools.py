# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_hook_delegate_effect prompt_delegate: what only Claude can do stays with Claude,
and an answer reads as onex's answer (OMN-26688).

Found in the interactive Claude Code test of 2026-11-26 (delegation switched on in
onex's /delegation tab, prompts typed into ``claude``). The prompt "Use the Agent
tool to launch one general-purpose subagent ..." was delegated, the model replied
"I can't launch an Agent tool or any subagent from here, ..." (run 664) and that
reply was shown to the user as the answer; the same prompt headless drew "I
cannot use the Agent tool ..." (run 654), which the delegate's gates happened to
veto. The answer itself opened under Claude Code's "UserPromptSubmit operation
blocked by hook:" with nothing saying who answered until a bracketed footer,
which also carried a /workflow?correlation_id= fragment nobody can open.

Failure modes, each caught below:

  T1  a prompt that tells Claude to use one of its tools or launch a (sub)agent is
      sent to a model that has no tools.
  T2  a reply that says the model cannot do the task is shown as the answer.
  T3  T1 or T2 over-reach: a question about agents, or an answer that merely
      starts "I can't think of ...", no longer reaches the user from onex.
  W1  the answer does not say first who answered and that Claude was not used.
  W2  the dead /workflow? fragment is still shown, or the run, the dashboard
      command or the @claude way out is lost.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_hook_delegate_effect.models import EnumPromptDelegateOutcome
from omnimarket.nodes.node_hook_delegate_effect.tests.test_prompt_delegate import (
    _ANSWER,
    _ask,
    _Cli,
    _doc,
)

pytestmark = pytest.mark.unit

#: The two replies recorded on 2026-11-26 (delegation store rows 664 and 654).
_RECORDED_REFUSALS = [
    "I can't launch an Agent tool or any subagent from here, so I can't run that "
    "task or repeat a subagent's answer. If you'd like, I can just answer it "
    "directly: a Python dictionary is a mutable, hash-based mapping of unique keys "
    "to values, allowing fast lookup, insertion, and deletion of key-value pairs.",
    "I cannot use the Agent tool or launch subagents; I can only answer directly. "
    "A Python dictionary is a built-in mutable mapping data type that stores "
    "key-value pairs, allowing fast lookup, insertion, and deletion of values by "
    "their unique keys.",
]


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.mark.parametrize(
    "prompt",
    [
        # The prompt typed in the interactive test (run 664).
        "Use the Agent tool to launch one general-purpose subagent with this task: "
        "'In one sentence, explain what a Python dictionary is.' Then repeat its "
        "answer to me word for word.",
        "Spawn two subagents to compare these approaches",
        "Please use the Bash tool to list the open ports",
        "Launch an agent that summarises the README",
    ],
)
def test_a_request_for_claudes_tools_stays_with_claude(prompt: str) -> None:
    """T1."""
    cli = _Cli(_doc(_ANSWER, "r-tools"))
    result = _ask(cli, prompt)
    assert result.outcome is EnumPromptDelegateOutcome.CONTROL
    assert result.hook_output == ""
    assert cli.calls == []


@pytest.mark.parametrize(
    "reply",
    [
        *_RECORDED_REFUSALS,
        "Sorry, I'm unable to access your repository from here.",
        "I don't have access to run commands, but in general a port is ...",
    ],
)
def test_a_reply_that_cannot_do_the_task_passes_to_claude(reply: str) -> None:
    """T2: the prompt goes on to Claude, naming the model and run that declined."""
    cli = _Cli(_doc(reply, "r-cannot"))
    result = _ask(cli, "Explain what a Python dictionary is, with one example")
    assert result.outcome is EnumPromptDelegateOutcome.DECLINED
    assert result.hook_output == ""
    assert result.model == "Qwen3.8-27B"
    assert result.run_id == "r-cannot"


@pytest.mark.parametrize(
    ("prompt", "reply"),
    [
        ("What is a subagent in Claude Code?", _ANSWER),
        ("How do agents use tools?", _ANSWER),
        (
            "What is the fastest way to look up a key?",
            "I can't think of anything faster than a hash map: use a dict.",
        ),
    ],
)
def test_questions_about_agents_and_ordinary_answers_still_come_from_onex(
    prompt: str, reply: str
) -> None:
    """T3: the controls for T1 and T2."""
    cli = _Cli(_doc(reply, "r-fine"))
    result = _ask(cli, prompt)
    assert result.outcome is EnumPromptDelegateOutcome.ANSWERED
    assert len(cli.calls) == 1


def test_the_answer_says_first_who_answered() -> None:
    """W1, W2."""
    correlation = "c079f9fe-51a2-425c-b39d-5f55ba29469b"
    doc = _doc(_ANSWER, "r-words")
    result_block = doc["result"]
    assert isinstance(result_block, dict)
    result_block["correlation_id"] = correlation
    result = _ask(_Cli(doc), "Explain how a mutex differs from a semaphore")
    reason = json.loads(result.hook_output)["reason"]
    lines = reason.splitlines()
    assert lines[0] == "Answered by onex with Qwen3.8-27B. Claude was not used."
    assert lines[1] == ""
    assert lines[2] == _ANSWER
    assert "/workflow?" not in reason
    assert "run r-words" in reason
    assert f"onex dashboard open --correlation-id {correlation}" in reason
    assert reason.rstrip().endswith(
        "To ask Claude instead, start your prompt with @claude."
    )


def test_without_a_correlation_the_run_is_still_named() -> None:
    """W2."""
    result = _ask(_Cli(_doc(_ANSWER, "r-bare")), "Explain what a binary search is")
    reason = json.loads(result.hook_output)["reason"]
    assert "run r-bare" in reason
    assert "onex dashboard open" not in reason
