# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each delegation hook has its own switch (OMN-23413, operator 2026-11-15).

Operator, 2026-11-15: "how to enable and disable separate hooks?" then "build
separate switches". Both hooks stay installed; ~/.onex/delegation.json says
which of them delegates, so pausing one never edits Claude Code's settings.

Failure modes:
  W1  the prompt switch off still delegates prompts, or pauses subagent tasks.
  W2  the subagent switch off still delegates subagent tasks, or pauses prompts.
  W3  no switch file is read as off (an install from before the switches
      must keep delegating both).
  W4  a switch file onex cannot read is read as on: delegation the user may
      have paused would run.
  W5  writing the switches leaves a partial file, or loses the other switch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_hook_delegate_effect.container.container_hook_delegate import (
    build_handler,
    build_prompt_handler,
)
from omnimarket.nodes.node_hook_delegate_effect.handlers.handler_hook_delegate import (
    read_switches,
    switches_path,
    write_switches,
)
from omnimarket.nodes.node_hook_delegate_effect.models import (
    EnumDelegateCliState,
    EnumHookDelegateOutcome,
    EnumPromptDelegateOutcome,
    ModelDelegateCliRun,
    ModelDelegationSwitches,
    ModelHookDelegateRequest,
    ModelPromptDelegateRequest,
)

pytestmark = pytest.mark.unit

_ANSWER = json.dumps(
    {"run_id": "r-1", "result": {"response": "TCP is reliable.", "model_name": "M"}}
)


class _Cli:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str], timeout_seconds: float) -> ModelDelegateCliRun:
        self.calls.append(args)
        return ModelDelegateCliRun(
            state=EnumDelegateCliState.EXITED, exit_code=0, stdout=_ANSWER
        )


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))


def _prompt(cli: _Cli) -> EnumPromptDelegateOutcome:
    request = ModelPromptDelegateRequest(
        prompt="How do TCP and UDP differ?", session_id="s"
    )
    return build_prompt_handler(cli, lambda _text: False).handle(request).outcome


def _subagent(cli: _Cli) -> EnumHookDelegateOutcome:
    request = ModelHookDelegateRequest.of(
        {"prompt": "explain TCP"}, "explain TCP", "research", 30
    )
    return build_handler(cli).handle(request).outcome


def test_the_prompt_switch_pauses_only_prompts() -> None:
    """W1."""
    write_switches(ModelDelegationSwitches(prompts=False, subagents=True))
    cli = _Cli()
    assert _prompt(cli) is EnumPromptDelegateOutcome.PAUSED
    assert cli.calls == []
    assert _subagent(cli) is EnumHookDelegateOutcome.ANSWERED
    assert len(cli.calls) == 1


def test_the_subagent_switch_pauses_only_subagent_tasks() -> None:
    """W2."""
    write_switches(ModelDelegationSwitches(prompts=True, subagents=False))
    cli = _Cli()
    assert _subagent(cli) is EnumHookDelegateOutcome.PAUSED
    assert cli.calls == []
    assert _prompt(cli) is EnumPromptDelegateOutcome.ANSWERED
    assert len(cli.calls) == 1


def test_no_switch_file_delegates_both() -> None:
    """W3."""
    assert not switches_path().exists()
    assert read_switches() == ModelDelegationSwitches(prompts=True, subagents=True)
    cli = _Cli()
    assert _prompt(cli) is EnumPromptDelegateOutcome.ANSWERED
    assert _subagent(cli) is EnumHookDelegateOutcome.ANSWERED


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"prompts": "yes"}'])
def test_a_switch_file_onex_cannot_read_delegates_nothing(content: str) -> None:
    """W4."""
    path = switches_path()
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert read_switches() == ModelDelegationSwitches(prompts=False, subagents=False)
    cli = _Cli()
    assert _prompt(cli) is EnumPromptDelegateOutcome.PAUSED
    assert _subagent(cli) is EnumHookDelegateOutcome.PAUSED
    assert cli.calls == []


def test_writing_one_switch_keeps_the_other() -> None:
    """W5."""
    write_switches(ModelDelegationSwitches(prompts=False, subagents=True))
    write_switches(read_switches().model_copy(update={"subagents": False}))
    assert json.loads(switches_path().read_text()) == {
        "prompts": False,
        "subagents": False,
    }
    assert [p.name for p in switches_path().parent.iterdir()] == ["delegation.json"]
