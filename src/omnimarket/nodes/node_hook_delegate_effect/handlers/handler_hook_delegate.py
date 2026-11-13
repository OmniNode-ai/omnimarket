# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer a delegated Claude Code subagent task with `onex delegate`.

omniclaude's PreToolUse delegation hook used to classify a task, print a
routing label and exit 2, so Claude Code blocked the subagent and no model was
called. The hook now hands a delegatable task to this handler: only an
ANSWERED result blocks the subagent, and every other outcome passes the task
through to Claude Code, so a subagent is never blocked without an answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnimarket.nodes.node_hook_delegate_effect.models import (
    EnumDelegateCliState,
    EnumHookDelegateOutcome,
    ModelHookDelegateRequest,
    ModelHookDelegateResult,
)
from omnimarket.nodes.node_hook_delegate_effect.protocols.protocol_delegate_cli import (
    ProtocolDelegateCli,
)

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer

#: Written by `onex claude enable`. While it says enabled, the ONEX gateway
#: already routes the session, so the hook delegates nothing.
GATEWAY_JOURNAL = Path(".onex") / "claude_gateway.json"

#: The classifier's intent as an `onex delegate` task class. Without one,
#: onex delegate guesses, and a prose question judged as code_generation was
#: vetoed for not compiling. An intent with no matching public class (debug,
#: database, unknown) is left for onex delegate to resolve.
TASK_CLASS_BY_INTENT = {
    "implement": "code_generation",
    "refactor": "refactor",
    "research": "research",
    "test": "test",
    "document": "document",
}

CLI_PROTOCOL: type[Any] = ProtocolDelegateCli

_PASS_THROUGH = {
    EnumDelegateCliState.NOT_FOUND: EnumHookDelegateOutcome.NO_ONEX,
    EnumDelegateCliState.TIMED_OUT: EnumHookDelegateOutcome.TIMED_OUT,
    EnumDelegateCliState.NOT_STARTED: EnumHookDelegateOutcome.FAILED,
}


def gateway_owns_session(home: Path) -> bool:
    """The gateway journal says enabled; an unreadable journal counts as enabled."""
    try:
        return '"phase":"enabled"' in (home / GATEWAY_JOURNAL).read_text()
    except FileNotFoundError:
        return False
    except OSError:
        return True


class HandlerHookDelegate:
    """Runs `onex delegate <task> --json` through the container's ProtocolDelegateCli."""

    def __init__(self, container: ModelONEXContainer) -> None:
        self._container = container

    def handle(self, request: ModelHookDelegateRequest) -> ModelHookDelegateResult:
        if gateway_owns_session(Path.home()):
            return ModelHookDelegateResult(
                outcome=EnumHookDelegateOutcome.GATEWAY_OWNS_SESSION
            )
        args = ["delegate", request.task, "--json"]
        task_class = TASK_CLASS_BY_INTENT.get(request.intent)
        if task_class:
            args += ["--task-type", task_class]
        cli: ProtocolDelegateCli = self._container.get_service(CLI_PROTOCOL)
        ran = cli.run(args, request.timeout_seconds)
        if ran.state in _PASS_THROUGH:
            return ModelHookDelegateResult(outcome=_PASS_THROUGH[ran.state])
        if ran.exit_code != 0:
            return ModelHookDelegateResult(outcome=EnumHookDelegateOutcome.FAILED)
        return read_answer(ran.stdout)


def read_answer(stdout: str) -> ModelHookDelegateResult:
    """The answer in the last JSON object line of `onex delegate --json`."""
    for line in reversed(stdout.splitlines()):
        try:
            doc = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        result = doc.get("result")
        if not isinstance(result, dict):
            break
        answer = result.get("response")
        if not isinstance(answer, str) or not answer.strip():
            break
        return ModelHookDelegateResult(
            outcome=EnumHookDelegateOutcome.ANSWERED,
            answer=answer,
            model=str(result.get("model_name") or ""),
            run_id=str(doc.get("run_id") or ""),
        )
    return ModelHookDelegateResult(outcome=EnumHookDelegateOutcome.NO_ANSWER)
