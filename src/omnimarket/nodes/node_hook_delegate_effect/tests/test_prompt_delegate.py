# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_hook_delegate_effect prompt_delegate: every prompt goes to delegation first (OMN-23413 AC33).

Spec: OMN-23413 Amendment 9. Failure modes, each caught below:

  P1  a prompt the classifier would reject is not delegated (AC33 says every prompt).
  P2  a model that cannot answer still replaces Claude (a decline must pass through).
  P3  the model gets the bare prompt, without the rules or the recent conversation,
      so follow-ups lose their meaning.
  P4  "@claude" at the start of a prompt still delegates it.
  P5  a slash command or a sensitive prompt is sent to a model.
  P6  Claude never learns what delegation answered while it was blocked.
  P7  that history repeats or grows without bound.
  S1  an accepted answer only adds context, so Claude still runs (and is billed)
      for the same prompt. decision "block" makes Claude Code skip the model call
      (verified 2026-11-13: num_turns 0, total_cost_usd 0).
  S2  a prompt that passes through prints anything but context for Claude.
  L1  the prompt is graded by the task class's rubric: a short decline was
      vetoed and retried down the ladder (run a165bc2e: 4 attempts, 48 s) and a
      one-sentence answer took 63 s and about 6000 tokens (run 18cdbca6).
  L2  a prompt waits as long as a subagent task (240 s).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_hook_delegate_effect.container import build_prompt_handler
from omnimarket.nodes.node_hook_delegate_effect.handlers.handler_prompt_delegate import (
    DECLINE_TOKEN,
    HISTORY_CHARS,
    PROMPT_CRITERIA,
    PROMPT_TIMEOUT_S,
)
from omnimarket.nodes.node_hook_delegate_effect.models import (
    EnumDelegateCliState,
    EnumPromptDelegateOutcome,
    ModelDelegateCliRun,
    ModelPromptDelegateRequest,
    ModelPromptDelegateResult,
)

pytestmark = pytest.mark.unit

_ANSWER = "TCP is connection-oriented and reliable; UDP is connectionless and faster."


def _doc(answer: str, run: str, model: str = "Qwen3.8-27B") -> dict[str, object]:
    return {"run_id": run, "result": {"response": answer, "model_name": model}}


def _vetoed(raw: str, run: str) -> dict[str, object]:
    """A decline onex delegate's gates vetoed: the reply lives only in raw_response."""
    evidence = {"raw_response": {"text": raw}}
    return {
        "run_id": run,
        "status": "failed",
        "receipt": {
            "status": "failed",
            "result": {
                "handler_result": {
                    "response": "",
                    "response_contract_evidence": evidence,
                }
            },
        },
    }


class _Cli:
    """Answers from ``docs`` in turn and records every call."""

    def __init__(self, *docs: dict[str, object]) -> None:
        self.docs = list(docs)
        self.calls: list[tuple[list[str], float]] = []

    def run(self, args: list[str], timeout_seconds: float) -> ModelDelegateCliRun:
        self.calls.append((args, timeout_seconds))
        if not self.docs:
            return ModelDelegateCliRun(state=EnumDelegateCliState.TIMED_OUT)
        doc = self.docs[min(len(self.calls), len(self.docs)) - 1]
        return ModelDelegateCliRun(
            state=EnumDelegateCliState.EXITED,
            exit_code=1 if doc.get("status") == "failed" else 0,
            stdout=json.dumps(doc),
        )


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _ask(cli: _Cli, prompt: str, **fields: Any) -> ModelPromptDelegateResult:
    request = ModelPromptDelegateRequest(
        prompt=prompt, session_id=str(fields.pop("session_id", "ac33")), **fields
    )
    return build_prompt_handler(cli).handle(request)


def _arg(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _task(argv: list[str]) -> str:
    return _arg(argv, "delegate")


# --- delegating ---------------------------------------------------------------


def test_any_prompt_is_delegated_and_its_answer_blocks_claude() -> None:
    """P1, S1: "hi" has no delegatable intent; it is delegated anyway, and the
    accepted answer blocks the prompt with the answer, model and run id."""
    cli = _Cli(_doc("Hello! How can I help?", "r-hi"))
    result = _ask(cli, "hi")
    assert result.outcome is EnumPromptDelegateOutcome.ANSWERED
    out = json.loads(result.hook_output)
    assert out["decision"] == "block"
    assert out["reason"].startswith("Hello! How can I help?")
    assert "Qwen3.8-27B" in out["reason"]
    assert "run r-hi" in out["reason"]
    assert "@claude" in out["reason"]
    ((argv, _),) = cli.calls
    assert _arg(argv, "--task-type") == "research"


@pytest.mark.parametrize(
    "reply",
    [
        DECLINE_TOKEN,
        f"{DECLINE_TOKEN}: needs the user's files",
        # Qwen3.8-27B reasoned aloud and put it inline at the end (run 33bab27a).
        f"This needs local files. Therefore the response is `{DECLINE_TOKEN}: needs files`.",
    ],
)
def test_a_declined_prompt_passes_to_claude(reply: str) -> None:
    """P2: a bare token, the token with a reason, or the token inline at the end."""
    result = _ask(_Cli(_doc(reply, "r-no")), "what does notes.txt say?")
    assert result.outcome is EnumPromptDelegateOutcome.DECLINED
    assert result.hook_output == ""


def test_a_decline_the_quality_gate_vetoed_passes_to_claude() -> None:
    """P2: onex delegate's gates veto short declines (runs 05a7be20, 0e876819), so
    the run fails with the reply only in raw_response."""
    cli = _Cli(_vetoed(f"### ANSWER\n{DECLINE_TOKEN}: needs files", "r-vetoed"))
    result = _ask(cli, "what does notes.txt say?")
    assert result.outcome is EnumPromptDelegateOutcome.DECLINED
    assert result.run_id == "r-vetoed"


def test_prose_that_merely_mentions_the_token_is_an_answer() -> None:
    """P2 control: a decline is declared, never inferred from prose."""
    prose = f"The string {DECLINE_TOKEN} is a sentinel some tools use."
    result = _ask(_Cli(_doc(prose, "r-prose")), "what is a sentinel value?")
    assert result.outcome is EnumPromptDelegateOutcome.ANSWERED


def test_a_failed_or_timed_out_delegation_passes_to_claude() -> None:
    result = _ask(_Cli(), "TCP vs UDP?")
    assert result.outcome is EnumPromptDelegateOutcome.NOT_ANSWERED
    assert result.detail == "timed_out"
    assert result.hook_output == ""


def test_the_model_gets_the_rules_the_prompt_and_recent_conversation(
    tmp_path: Path,
) -> None:
    """P3."""
    transcript = tmp_path / "transcript.jsonl"
    rows = [
        {"type": "user", "message": {"content": "Tell me about TCP."}},
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "TCP is a transport protocol."}]
            },
        },
        {"type": "user", "isMeta": True, "message": {"content": "meta row"}},
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    cli = _Cli(_doc(_ANSWER, "r-ctx"))
    _ask(cli, "and how is UDP different?", transcript_path=str(transcript))
    ((argv, _),) = cli.calls
    assert "and how is UDP different?" in _task(argv)
    assert "TCP is a transport protocol." in _task(argv)
    assert "meta row" not in _task(argv)
    assert f"{DECLINE_TOKEN}: " in _arg(argv, "--system-prompt")
    assert DECLINE_TOKEN not in _task(argv)


def test_a_prompt_brings_its_own_bar_token_cap_and_time_limit() -> None:
    """L1, L2: the prompt's own bar answered both cases on the first attempt in
    22 s and 17 s (runs 6ec07c1f, 251f59de)."""
    cli = _Cli(_doc(_ANSWER, "r-bar"))
    _ask(cli, "TCP vs UDP?", intent="implement", timeout_seconds=240)
    ((argv, limit),) = cli.calls
    criteria = [argv[i + 1] for i, a in enumerate(argv) if a == "--criteria"]
    assert tuple(criteria) == PROMPT_CRITERIA
    assert _arg(argv, "--criteria-mode") == "replace-task-class"
    assert int(_arg(argv, "--max-tokens")) <= 2048
    assert _arg(argv, "--task-type") == "code_generation"
    assert limit == PROMPT_TIMEOUT_S


def test_a_shorter_hook_limit_wins() -> None:
    cli = _Cli(_doc(_ANSWER, "r-limit"))
    _ask(cli, "TCP vs UDP?", timeout_seconds=5)
    assert cli.calls[0][1] == 5


@pytest.mark.parametrize(
    ("prompt", "fields", "outcome"),
    [
        ("@claude explain TCP", {}, EnumPromptDelegateOutcome.SKIPPED),
        ("  @Claude explain TCP", {}, EnumPromptDelegateOutcome.SKIPPED),
        ("/model", {}, EnumPromptDelegateOutcome.SLASH_COMMAND),
        ("   ", {}, EnumPromptDelegateOutcome.EMPTY),
        ("my key is sk-...", {"sensitive": True}, EnumPromptDelegateOutcome.SENSITIVE),
    ],
)
def test_some_prompts_go_to_claude_without_delegating(
    prompt: str, fields: dict[str, object], outcome: EnumPromptDelegateOutcome
) -> None:
    """P4, P5."""
    cli = _Cli(_doc(_ANSWER, "r-skip"))
    result = _ask(cli, prompt, **fields)
    assert result.outcome is outcome
    assert result.hook_output == ""
    assert cli.calls == []


# --- what Claude learns ------------------------------------------------------------


def test_claude_is_told_once_what_delegation_answered_while_blocked() -> None:
    """P6, P7: the next prompt that reaches Claude carries the delegated
    exchange as context, once; the delegated model sees it too."""
    cli = _Cli(
        _doc(_ANSWER, "r-1"), _doc(DECLINE_TOKEN, "r-2"), _doc(DECLINE_TOKEN, "r-3")
    )
    _ask(cli, "TCP vs UDP?")
    first = _ask(cli, "now edit my file")
    assert first.outcome is EnumPromptDelegateOutcome.DECLINED
    out = json.loads(first.hook_output)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "TCP vs UDP?" in context
    assert _ANSWER in context
    assert "r-1" in context
    assert "decision" not in out
    assert _ANSWER in _task(cli.calls[1][0])
    again = _ask(cli, "and run the tests")
    assert again.hook_output == ""


def test_the_same_run_is_kept_once_and_sessions_do_not_mix() -> None:
    cli = _Cli(_doc(_ANSWER, "r-same"), _doc(_ANSWER, "r-same"))
    _ask(cli, "TCP vs UDP?")
    _ask(cli, "TCP vs UDP, again?")
    context = _ask(_Cli(), "edit my file").hook_output
    assert (
        json.loads(context)["hookSpecificOutput"]["additionalContext"].count("r-same")
        == 1
    )
    assert _ask(_Cli(), "edit my file", session_id="other").hook_output == ""


def test_the_history_given_to_claude_is_bounded() -> None:
    """P7: many long delegated answers never produce unbounded context."""
    cli = _Cli(*[_doc("x" * 5000, f"r-{i}") for i in range(10)])
    for i in range(10):
        _ask(cli, f"question {i}")
    out = json.loads(_ask(_Cli(), "now edit my file").hook_output)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert 0 < len(context) <= HISTORY_CHARS
    assert "question 9" in context  # the newest exchange is kept
    assert "question 0" not in context
