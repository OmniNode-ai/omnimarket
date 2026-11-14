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

import fcntl
import hashlib
import json
import os
import re
import tempfile
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
        return bool(
            json.loads((home / GATEWAY_JOURNAL).read_text()).get("phase") == "enabled"
        )
    except FileNotFoundError:
        return False
    except (OSError, ValueError, AttributeError):
        return True


class HandlerHookDelegate:
    """Runs `onex delegate <task> --json` through the container's ProtocolDelegateCli."""

    def __init__(self, container: ModelONEXContainer) -> None:
        self._container = container

    @staticmethod
    def _turn_file(session_id: str) -> Path:
        root = Path(os.environ.get("ONEX_STATE_DIR", str(Path.home() / ".onex_state")))
        return (
            root
            / "claude_hook_turns"
            / (hashlib.sha256(session_id.encode()).hexdigest() + ".json")
        )

    @staticmethod
    def _save_turn(path: Path, answers: dict[str, Any]) -> None:
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(answers, stream)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def begin_turn(self, session_id: str) -> None:
        """Even an ineligible new prompt invalidates the previous turn's answers."""
        if not session_id:
            return
        path = self._turn_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._save_turn(path, {})

    def handle(self, request: ModelHookDelegateRequest) -> ModelHookDelegateResult:
        if gateway_owns_session(Path.home()):
            return ModelHookDelegateResult(
                outcome=EnumHookDelegateOutcome.GATEWAY_OWNS_SESSION
            )
        path = self._turn_file(request.session_id)
        if not request.session_id or not path.is_file():
            return self._delegate(request)
        # Only the hook path owns this cache. Different sessions do not share
        # state; a session lock prevents concurrent identical tasks running twice.
        with open(path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                answers = json.loads(path.read_text())
                if not isinstance(answers, dict):
                    answers = {}
            except (OSError, ValueError):
                answers = {}
            key = hashlib.sha256(request.task.strip().encode()).hexdigest()
            if key in answers:
                cached = ModelHookDelegateResult.model_validate(answers[key])
                if cached.answered:
                    return cached
            result = self._delegate(request)
            if result.answered:
                answers[key] = result.model_dump(mode="json")
                self._save_turn(path, answers)
            return result

    def _delegate(self, request: ModelHookDelegateRequest) -> ModelHookDelegateResult:
        args = ["delegate", request.task, "--json"]
        task_class = TASK_CLASS_BY_INTENT.get(request.intent)
        if task_class:
            args += ["--task-type", task_class]
        if request.system_prompt:
            args += ["--system-prompt", request.system_prompt]
        for slug in request.criteria:
            args += ["--criteria", slug]
        if request.criteria and request.criteria_mode:
            args += ["--criteria-mode", request.criteria_mode]
        if request.max_tokens is not None:
            args += ["--max-tokens", str(request.max_tokens)]
        cli: ProtocolDelegateCli = self._container.get_service(CLI_PROTOCOL)
        ran = cli.run(args, request.timeout_seconds)
        if ran.state in _PASS_THROUGH:
            return ModelHookDelegateResult(outcome=_PASS_THROUGH[ran.state])
        declined = read_decline(ran.stdout, request.decline_token)
        if declined is not None:
            return declined
        if ran.exit_code != 0:
            return ModelHookDelegateResult(outcome=EnumHookDelegateOutcome.FAILED)
        return read_answer(ran.stdout)


#: How deep read_decline looks for the model's raw reply inside a receipt.
_DECLINE_SEARCH_DEPTH = 8


def _declares(text: str, token: str) -> bool:
    """The reply declares the token: "token:" anywhere, or the token alone on a line.

    Models that reason aloud put the decline inline at the end (Qwen3.8-27B,
    run 33bab27a: 'the response is `TOKEN: needs the user's files`'). A passing
    mention of the token without its colon is not a decline.
    """
    if re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}\s*:", text):
        return True
    return any(
        line.strip().strip("`*_>.").strip() == token for line in text.splitlines()
    )


def _replies(node: object, depth: int = 0) -> list[str]:
    """Every raw_response.text and response string in a receipt, outermost first."""
    if depth > _DECLINE_SEARCH_DEPTH:
        return []
    found: list[str] = []
    if isinstance(node, dict):
        raw = node.get("raw_response")
        if isinstance(raw, dict) and isinstance(raw.get("text"), str):
            found.append(raw["text"])
        if isinstance(node.get("response"), str):
            found.append(node["response"])
        for value in node.values():
            found += _replies(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            found += _replies(value, depth + 1)
    return found


def _model_of(doc: dict[str, Any]) -> str:
    for node in (doc, doc.get("receipt"), (doc.get("receipt") or {}).get("result")):
        if isinstance(node, dict) and isinstance(node.get("model_name"), str):
            return str(node["model_name"])
    for attempt in doc.get("attempts") or []:
        if isinstance(attempt, dict) and isinstance(attempt.get("model_id"), str):
            return str(attempt["model_id"])
    return ""


def read_decline(stdout: str, token: str) -> ModelHookDelegateResult | None:
    """DECLINED when the model's reply declares ``token`` (OMN-23413 AC33).

    onex delegate's quality gates veto a short decline (a bare one-word reply,
    a refusal phrase, a task mismatch), so the run exits failed with the reply
    kept only in the receipt's raw_response. Prose that merely mentions the
    token is not a decline. With no token, nothing is ever declined.
    """
    if not token:
        return None
    for line in reversed(stdout.splitlines()):
        try:
            doc = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        if not any(_declares(reply, token) for reply in _replies(doc)):
            return None
        receipt = doc.get("receipt")
        run_id = doc.get("run_id") or (
            receipt.get("run_id") if isinstance(receipt, dict) else ""
        )
        return ModelHookDelegateResult(
            outcome=EnumHookDelegateOutcome.DECLINED,
            model=_model_of(doc),
            run_id=str(run_id or ""),
        )
    return None


def read_answer(stdout: str) -> ModelHookDelegateResult:
    """The answer in the last JSON object line of `onex delegate --json`."""
    for line in reversed(stdout.splitlines()):
        try:
            doc = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        receipt = doc.get("receipt", doc)
        if not isinstance(receipt, dict):
            break
        if (
            doc.get("status", "success") != "success"
            or receipt.get("status", "success") != "success"
        ):
            break
        result = receipt.get("result")
        if not isinstance(result, dict):
            break
        if (
            result.get("status", "completed") != "completed"
            or result.get("quality_gate_passed") is False
        ):
            break
        answer = result.get("response")
        if not isinstance(answer, str) or not answer.strip():
            break
        model = result.get("model_name")
        run_id = doc.get("run_id") or receipt.get("run_id")
        if (
            not isinstance(model, str)
            or not model
            or not isinstance(run_id, str)
            or not run_id
        ):
            break
        return ModelHookDelegateResult(
            outcome=EnumHookDelegateOutcome.ANSWERED,
            answer=answer,
            model=model,
            run_id=run_id,
        )
    return ModelHookDelegateResult(outcome=EnumHookDelegateOutcome.NO_ANSWER)
