# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Accepted answers are reused only within one prompt turn and session.

The failure we catch is two CLI invocations for the same prompt, first through
UserPromptSubmit and then through Agent/Task. A later user turn must run again.
"""

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_hook_delegate_effect.container import build_handler
from omnimarket.nodes.node_hook_delegate_effect.models import (
    EnumDelegateCliState,
    ModelDelegateCliRun,
    ModelHookDelegateRequest,
)

pytestmark = pytest.mark.unit


class _Cli:
    def __init__(self, success: bool = True) -> None:
        self.calls = 0
        self.success = success

    def run(self, args: list[str], timeout_seconds: float) -> ModelDelegateCliRun:
        self.calls += 1
        return ModelDelegateCliRun(
            state=EnumDelegateCliState.EXITED,
            exit_code=0 if self.success else 1,
            stdout=json.dumps(
                {
                    "run_id": f"run-{self.calls}",
                    "result": {"response": "Accepted answer", "model_name": "chosen"},
                }
            ),
        )


def _request(session: str = "one") -> ModelHookDelegateRequest:
    return ModelHookDelegateRequest(
        task="Explain a list comprehension", timeout_seconds=5, session_id=session
    )


def test_prompt_and_identical_subagent_task_use_one_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    cli = _Cli()
    handler = build_handler(cli)
    handler.begin_turn("one")
    prompt = handler.handle(_request())
    tool = handler.handle(_request())
    assert prompt.answered
    assert tool == prompt
    assert cli.calls == 1
    handler.begin_turn("one")
    assert handler.handle(_request()).run_id != prompt.run_id
    assert cli.calls == 2


def test_other_sessions_and_failed_runs_are_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    cli = _Cli(success=False)
    handler = build_handler(cli)
    handler.begin_turn("one")
    assert not handler.handle(_request()).answered
    cli.success = True
    assert handler.handle(_request()).answered
    handler.begin_turn("two")
    assert handler.handle(_request("two")).answered
    assert cli.calls == 3


def test_subagent_without_a_prompt_turn_keeps_normal_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    cli = _Cli()
    handler = build_handler(cli)
    assert handler.handle(_request()).answered
    assert handler.handle(_request()).answered
    assert cli.calls == 2
