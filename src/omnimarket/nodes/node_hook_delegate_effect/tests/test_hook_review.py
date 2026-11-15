# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_hook_delegate_effect: review findings, seen in review 2026-11-15 (OMN-23413).

Failure modes, each caught below:
  R1  the sensitivity check sees the conversation only after it is cut to
      CONTEXT_CHARS, so a cut that starts inside "password" or one character
      into an sk- key sends the secret with the prompt.
  R3  with no sensitivity check given (a runner older than the argument), the
      conversation is sent unchecked and the log line does not say so.
  R4  a decline spelled with Markdown, in lower case, or with a dash or bracket
      after the token is read as an answer: it blocks the real subagent.
  R5  the session lock is held for the whole `onex delegate` run, so a session's
      subagent tasks run one after another and a new prompt's turn reset waits
      for them; an identical task declined earlier in the turn is delegated again.
  R6  a leftover ~/.onex/claude_gateway.json saying enabled stops delegation
      again (the gateway is gone, Amendment 11); a check that does not flag
      still withholds the conversation.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest

from omnimarket.nodes.node_hook_delegate_effect.container import (
    build_handler,
    build_prompt_handler,
)
from omnimarket.nodes.node_hook_delegate_effect.handlers.handler_prompt_delegate import (
    CONTEXT_CHARS,
)
from omnimarket.nodes.node_hook_delegate_effect.models import (
    DECLINE_TOKEN,
    EnumDelegateCliState,
    EnumHookDelegateOutcome,
    EnumPromptDelegateOutcome,
    ModelDelegateCliRun,
    ModelHookDelegateRequest,
    ModelPromptDelegateRequest,
)

pytestmark = pytest.mark.unit

_ANSWER = "TCP is reliable; UDP is not."


def _gate(text: str) -> bool:
    """Like omniclaude's gate: a secret is recognised by its label or key prefix."""
    return bool(re.search(r"password\s*:|\bsk-ant-", text))


def _doc(reply: str, run: str = "r-1") -> dict[str, object]:
    return {"run_id": run, "result": {"response": reply, "model_name": "m"}}


class _Cli:
    """Answers with ``reply`` after ``delay`` seconds and records every call."""

    def __init__(self, reply: str = _ANSWER, delay: float = 0.0) -> None:
        self.reply = reply
        self.delay = delay
        self.calls: list[list[str]] = []

    def run(self, args: list[str], timeout_seconds: float) -> ModelDelegateCliRun:
        self.calls.append(args)
        time.sleep(self.delay)
        return ModelDelegateCliRun(
            state=EnumDelegateCliState.EXITED,
            exit_code=0,
            stdout=json.dumps(_doc(self.reply, f"r-{len(self.calls)}")),
        )


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    return home


def _transcript(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _cut_inside(tmp_path: Path, turn: str, cut_at: str) -> Path:
    """A transcript whose last CONTEXT_CHARS characters start at ``cut_at`` in ``turn``."""
    user = f"USER: {turn}"
    filler = CONTEXT_CHARS + user.index(cut_at) - len(user) - 1 - len("ASSISTANT: ")
    return _transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": turn}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "x" * filler}]},
            },
        ],
    )


def _prompt(transcript: Path, session: str = "s") -> ModelPromptDelegateRequest:
    return ModelPromptDelegateRequest(
        prompt="TCP vs UDP?", session_id=session, transcript_path=str(transcript)
    )


_KEY = "sk-ant-api03-" + "A1b2C3d4" * 10


@pytest.mark.parametrize(
    ("turn", "secret", "cut_at"),
    [
        (
            "for staging use password: hunter2-staging-pw",
            "hunter2-staging-pw",
            "word: hunter2",
        ),
        (f"my key is {_KEY} keep it safe", _KEY[1:], _KEY[1:]),
    ],
)
def test_a_cut_inside_a_secret_never_slips_past_the_check(
    tmp_path: Path, turn: str, secret: str, cut_at: str
) -> None:
    """R1: the check sees every turn the cut touches, whole."""
    cli = _Cli()
    handler = build_prompt_handler(cli, sensitive=_gate)
    result = handler.handle(_prompt(_cut_inside(tmp_path, turn, cut_at)))
    assert result.outcome is EnumPromptDelegateOutcome.ANSWERED
    ((argv),) = cli.calls
    assert secret not in argv[1]
    assert "TCP vs UDP?" in argv[1]
    assert "excerpt=withheld_sensitive" in result.log_line


def test_a_check_that_does_not_flag_keeps_the_conversation(tmp_path: Path) -> None:
    """R6: only a flagged conversation is withheld."""
    transcript = _transcript(
        tmp_path, [{"type": "user", "message": {"content": "Tell me about TCP."}}]
    )
    cli = _Cli()
    result = build_prompt_handler(cli, sensitive=lambda _: False).handle(
        _prompt(transcript)
    )
    assert "Tell me about TCP." in cli.calls[0][1]
    assert "excerpt=sent" in result.log_line


def test_without_a_check_the_conversation_is_not_sent(tmp_path: Path) -> None:
    """R3: fail closed, and say so in the log line."""
    transcript = _transcript(
        tmp_path,
        [{"type": "user", "message": {"content": "my db login is SECRET-VALUE"}}],
    )
    cli = _Cli()
    result = build_prompt_handler(cli).handle(_prompt(transcript))
    assert result.outcome is EnumPromptDelegateOutcome.ANSWERED
    assert "SECRET-VALUE" not in cli.calls[0][1]
    assert "TCP vs UDP?" in cli.calls[0][1]
    assert "excerpt=withheld_unchecked" in result.log_line


def _subagent(
    task: str = "read src/app.py", session: str = ""
) -> ModelHookDelegateRequest:
    return ModelHookDelegateRequest.of(
        {"prompt": task}, "{...}", "test", 240
    ).model_copy(update={"session_id": session})


@pytest.mark.parametrize(
    "reply",
    [
        f"`{DECLINE_TOKEN}`: needs the user's files",
        f"**{DECLINE_TOKEN}**: needs the user's files",
        f"{DECLINE_TOKEN.lower()}: needs the user's files",
        f"{DECLINE_TOKEN} - needs the user's files",
        f"{DECLINE_TOKEN} — needs the user's files",
        f"{DECLINE_TOKEN} (needs the user's files)",
        f"{DECLINE_TOKEN} [needs the user's files]",
        f"[{DECLINE_TOKEN}] needs the user's files",
        f"### ANSWER\n**{DECLINE_TOKEN}** - needs the user's files",
    ],
)
def test_a_decline_in_markdown_any_case_or_separator_is_declined(reply: str) -> None:
    """R4: the token opening the reply declines it however it is dressed."""
    result = build_handler(_Cli(reply)).handle(_subagent())
    assert result.outcome is EnumHookDelegateOutcome.DECLINED
    assert not result.answered


@pytest.mark.parametrize(
    "reply",
    [
        f"The marker {DECLINE_TOKEN} is what some tools print.",
        f"Use `{DECLINE_TOKEN}` as the sentinel - it is common.",
        f"{DECLINE_TOKEN.lower()} is a sentinel some tools print.",
        f"def test_it():\n    assert reply != 'x'  # {DECLINE_TOKEN.lower()} - not here",
    ],
)
def test_a_reply_that_only_mentions_the_token_stays_an_answer(reply: str) -> None:
    """R4 control."""
    assert build_handler(_Cli(reply)).handle(_subagent()).answered


def test_a_markdown_decline_passes_a_prompt_to_claude() -> None:
    """R4: the prompt path reads declines the same way."""
    cli = _Cli(f"**{DECLINE_TOKEN}**: needs the user's files")
    result = build_prompt_handler(cli, sensitive=_gate).handle(
        ModelPromptDelegateRequest(prompt="what does notes.txt say?", session_id="s")
    )
    assert result.outcome is EnumPromptDelegateOutcome.DECLINED


def test_a_sessions_subagent_tasks_are_delegated_side_by_side() -> None:
    """R5: three tasks of one turn, started together, take one run's time."""
    cli = _Cli(delay=0.5)
    handler = build_handler(cli)
    handler.begin_turn("s")
    threads = [
        threading.Thread(target=handler.handle, args=(_subagent(f"task {i}", "s"),))
        for i in range(3)
    ]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(cli.calls) == 3
    assert time.monotonic() - started < 1.2


def test_a_new_turn_never_waits_for_a_run_nor_inherits_its_answer() -> None:
    """R5: a run the new prompt overtook keeps nothing in the new turn."""
    reset: list[bool] = []
    prompts: list[threading.Thread] = []

    class _Overtaken(_Cli):
        """The user submits a new prompt while the first run is in flight."""

        def run(self, args: list[str], timeout_seconds: float) -> ModelDelegateCliRun:
            if not prompts:
                prompts.append(threading.Thread(target=handler.begin_turn, args=("s",)))
                prompts[0].start()
                prompts[0].join(2)
                reset.append(not prompts[0].is_alive())
            return super().run(args, timeout_seconds)

    cli = _Overtaken()
    handler = build_handler(cli)
    handler.begin_turn("s")
    assert handler.handle(_subagent("explain TCP", "s")).answered
    prompts[0].join()
    assert reset == [True]
    assert handler.handle(_subagent("explain TCP", "s")).answered
    assert len(cli.calls) == 2


def test_a_decline_is_reused_for_an_identical_task_in_the_turn() -> None:
    """R5."""
    cli = _Cli(f"{DECLINE_TOKEN}: needs the user's files")
    handler = build_handler(cli)
    handler.begin_turn("s")
    first = handler.handle(_subagent("read src/app.py", "s"))
    second = handler.handle(_subagent("read src/app.py", "s"))
    assert first.outcome is second.outcome is EnumHookDelegateOutcome.DECLINED
    assert len(cli.calls) == 1
    handler.begin_turn("s")
    handler.handle(_subagent("read src/app.py", "s"))
    assert len(cli.calls) == 2


@pytest.mark.parametrize("journal", ['{"phase": "enabled"}', "not json"])
def test_a_leftover_gateway_journal_does_not_stop_delegation(
    home: Path, journal: str
) -> None:
    """R6: the gateway is gone; its old journal, enabled or unreadable, is ignored."""
    (home / ".onex").mkdir()
    (home / ".onex" / "claude_gateway.json").write_text(journal)
    cli = _Cli()
    assert build_handler(cli).handle(_subagent("explain TCP")).answered
    prompt = build_prompt_handler(cli, sensitive=_gate).handle(
        ModelPromptDelegateRequest(prompt="TCP vs UDP?", session_id="s")
    )
    assert prompt.outcome is EnumPromptDelegateOutcome.ANSWERED
    assert len(cli.calls) == 2
