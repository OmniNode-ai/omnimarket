# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every Claude Code prompt goes to delegation first; Claude answers what it cannot.

OMN-23413 AC33. omniclaude's UserPromptSubmit hook hands each prompt here with
its sensitivity and intent. Slash commands, prompts that start with "@claude"
and sensitive prompts go straight to Claude. Any other prompt is delegated
through HandlerHookDelegate with the recent conversation, the prompt's own
rules and bar, and a decline token. An accepted answer blocks the prompt, so
Claude Code makes no model call, and shows the answer to the user. A decline or
any failure passes the prompt to Claude.

A blocked prompt never enters Claude's context, so accepted exchanges are kept
in a bounded per-session history, deduplicated by run id, and handed to Claude
once, on the next prompt that reaches it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnimarket.nodes.node_hook_delegate_effect.handlers.handler_hook_delegate import (
    HandlerHookDelegate,
    read_switches,
)
from omnimarket.nodes.node_hook_delegate_effect.models import (
    DECLINE_TOKEN,
    EnumHookDelegateOutcome,
    EnumPromptDelegateOutcome,
    EnumPromptExcerpt,
    ModelHookDelegateRequest,
    ModelPromptDelegateRequest,
    ModelPromptDelegateResult,
)

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer

SKIP_PREFIX = "@claude"
#: How Claude Code writes a pasted image into the prompt text; the image itself
#: is never in the hook's input, so a delegated model could only guess at it.
_IMAGE = re.compile(r"\[Image #\d+\]")
#: Words that approve or steer the session; the guards count an exact
#: "YES <id>" only when the session itself sees it (seen 2026-11-15: two
#: approvals and five "continue" were answered by a delegated model).
_CONTROL = re.compile(
    r"(?:yes|y|no|n|ok|okay|continue|cont|go|proceed|do it|stop|wait|retry|lgtm"
    r"|approve|approved|i\s+mean)\b",
    re.IGNORECASE,
)
# These messages direct the working session; an advice-only model must not
# consume them even when it can produce a plausible answer (2026-11-16).
_WORK_REQUEST = re.compile(
    r"(?:(?:please|now|and)\s+)*"
    r"(?:(?:can|could|would|will)\s+(?:you|we)\s+"
    r"|you\s+(?:can|could|should|may)\s+|help\s+me\s+"
    r"|i\s+(?:want|need)\s+you\s+to\s+)?(?:please\s+)?"
    r"(?:fix|check|inspect|update|edit|change|remove|delete|close|reopen|merge"
    r"|push|commit|deploy|install|uninstall|enable|disable|run|implement|build"
    r"|test|verify|read|open|move|revert|restore|coordinate|message|send"
    r"|approve|authorize|grant|allow)\b",
    re.IGNORECASE,
)
# Permission requests steer the operator's session. A delegate cannot approve
# an action for the person; conceptual permission questions remain eligible.
_PERMISSION_REQUEST = re.compile(
    r"(?:(?:one|a|single)\s+command\s+for\s+(?:all\s+)?(?:permissions?|approvals?)\b"
    r"|(?:can|may)\s+(?:i|we)\b"
    r"|(?:i|we)\s+(?:need|want|request|require)\s+(?:permission|approval|authorization)\b"
    r"|(?:generate|give|write|send)\b.*\b(?:permission|approval|authorization|YES)\b)",
    re.IGNORECASE,
)
# These questions need the working session's installation evidence.
_LOCAL_STATE_QUESTION = re.compile(
    r"(?:why|what|how|where|which)\b.*\b(?:my|our|this|current)\b.*"
    r"\b(?:installation|hook|dashboard|onex|claude|terminal|answer|text)\b"
    r"|why\b.*(?:\byellow\b.*\b(?:text|answer)\b"
    r"|\b(?:text|answer)\b.*\byellow\b)",
    re.IGNORECASE,
)
_CORRECTION = re.compile(
    r"\b(?:i(?:'ve|\s+have)?|we(?:'ve|\s+have)?)\s+(?:already\s+)?"
    r"(?:removed|changed|deleted|disabled|enabled|updated|decided|cancelled|canceled)\b",
    re.IGNORECASE,
)
_INFORMATION = re.compile(
    r"(?:what|why|how|when|where|who|which|explain|describe)\b", re.IGNORECASE
)
#: A prompt this short leans on the conversation ("fix all", "check and do").
_SHORT_WORDS = 3

#: How Claude Code opens a prompt the user did not type (read in this Mac's
#: transcripts 2026-11-15): peer-session messages, subagent reports, task and
#: system notifications, and the output of a local slash command.
_NOT_FROM_USER = re.compile(
    r"<(cross-session-message|agent-message|task-notification|local-command-caveat|"
    r"local-command-stdout|command-name)[\s>]|\[SYSTEM NOTIFICATION\b"
)
#: Recent conversation given to the delegated model.
CONTEXT_CHARS = 3000
#: Delegated history handed to Claude in one turn.
HISTORY_CHARS = 6000
#: Longest single prompt or answer kept in the history.
ANSWER_CHARS = 1500
HISTORY_KEEP = 8
#: Transcript lines read from the end for the recent conversation.
TRANSCRIPT_TAIL = 400

#: Match the ordinary delegate process budget; explicit shorter hook limits win.
PROMPT_TIMEOUT_S = 240.0

RULES = f"""You answer a user's message on behalf of a coding assistant (Claude Code).
- If answering needs the user's machine or accounts (reading or editing their files, running commands, checking Slack, Linear, GitHub, email, calendars or live system state), or needs earlier conversation you cannot see, or you are not confident the answer is correct, reply with exactly one line: {DECLINE_TOKEN}: followed by a reason of at most five words, such as "needs the user's files". That line is your whole reply: no explanation before or after it, no apology, never "I cannot".
- Otherwise give the answer directly and as briefly as the question allows; include code only when it is asked for.
- No preamble and no reasoning. Never claim to have run, edited or checked anything."""

_TASK = """Recent conversation (may be partial):
{context}

User message:
{prompt}
"""

_HISTORY_INTRO = (
    "Earlier in this session these prompts were answered by ONEX delegation "
    "and you did not see them:\n\n"
)


def skip_reason(prompt: str) -> EnumPromptDelegateOutcome | None:
    """Why this prompt goes to Claude without delegation, or None to delegate it."""
    text = prompt.lstrip()
    if not text:
        return EnumPromptDelegateOutcome.EMPTY
    if text.startswith("/"):
        return EnumPromptDelegateOutcome.SLASH_COMMAND
    if _NOT_FROM_USER.match(text):
        return EnumPromptDelegateOutcome.NOT_FROM_USER
    if text[: len(SKIP_PREFIX)].lower() == SKIP_PREFIX:
        return EnumPromptDelegateOutcome.SKIPPED
    if _IMAGE.search(text):
        return EnumPromptDelegateOutcome.HAS_IMAGE
    if _CONTROL.match(text) or len(text.split()) <= _SHORT_WORDS:
        return EnumPromptDelegateOutcome.CONTROL
    if (
        _WORK_REQUEST.match(text)
        or _LOCAL_STATE_QUESTION.match(text)
        or _PERMISSION_REQUEST.match(text)
    ):
        return EnumPromptDelegateOutcome.CONTROL
    if not _INFORMATION.match(text) and "?" not in text and _CORRECTION.search(text):
        return EnumPromptDelegateOutcome.CONTROL
    return None


def transcript_turns(path: str) -> list[str]:
    """The user and assistant text turns at the end of a Claude Code transcript."""
    if not path:
        return []
    try:
        lines = Path(path).read_text(errors="replace").splitlines()[-TRANSCRIPT_TAIL:]
    except OSError:
        return []
    turns: list[str] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("isMeta"):
            continue
        kind = row.get("type")
        if kind not in ("user", "assistant"):
            continue
        content = (row.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            continue
        text = text.strip()
        if text and not text.startswith("<"):
            turns.append(f"{kind.upper()}: {text}")
    return turns


def cut_turns(turns: list[str], limit: int = CONTEXT_CHARS) -> tuple[str, str]:
    """The last ``limit`` characters of ``turns``, and the text to check before sending them.

    The check sees every turn the cut touches, whole, and the turn before them:
    checked after the cut, a window starting inside "password" or one character
    into an sk- key hid the label or prefix the check needs, and the secret was
    sent (seen in review 2026-11-15, R1).
    """
    start, size = len(turns), -1
    while start > 0 and size < limit:
        start -= 1
        size += len(turns[start]) + 1
    return "\n".join(turns)[-limit:], "\n".join(turns[max(0, start - 1) :])


def transcript_excerpt(path: str, limit: int = CONTEXT_CHARS) -> str:
    """The last user and assistant text turns of a Claude Code transcript, unchecked."""
    return cut_turns(transcript_turns(path), limit)[0]


def block_output(answer: str, model: str, run_id: str, correlation_id: str = "") -> str:
    """decision "block" stops Claude Code before any model call (verified
    2026-11-13: num_turns 0, total_cost_usd 0) and shows the reason to the user."""
    reason = (
        answer.rstrip("\n") + f"\n\n[Answered by {model} through ONEX delegation, "
        f"run {run_id}"
        + (
            f", correlation {correlation_id}. Dashboard: /workflow?correlation_id={correlation_id}"
            f" (onex dashboard open --correlation-id {correlation_id})"
            if correlation_id
            else ""
        )
        + f". Start a prompt with {SKIP_PREFIX} to ask Claude instead.]"
    )
    return json.dumps({"decision": "block", "reason": reason})


def context_output(context: str) -> str:
    """Context for Claude, never the hook envelope; nothing when there is none."""
    if not context:
        return ""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }
    )


class HandlerPromptDelegate:
    """Delegates one prompt through HandlerHookDelegate and composes the hook output."""

    def __init__(
        self,
        container: ModelONEXContainer,
        sensitive: Callable[[str], bool] | None = None,
    ) -> None:
        self._delegate = HandlerHookDelegate(container)
        #: The caller's sensitivity check (omniclaude's gate). Recent conversation
        #: it flags is never sent: the prompt is delegated without it (OMN-23413).
        #: Without a check none is sent: a runner older than this argument must
        #: not send what it cannot check (review 2026-11-15, R3).
        self._sensitive = sensitive

    def handle(self, request: ModelPromptDelegateRequest) -> ModelPromptDelegateResult:
        skipped = skip_reason(request.prompt)
        if skipped is not None:
            return self._pass(request.session_id, skipped)
        if not read_switches().prompts:
            return self._pass(request.session_id, EnumPromptDelegateOutcome.PAUSED)
        if request.sensitive:
            return self._pass(request.session_id, EnumPromptDelegateOutcome.SENSITIVE)
        hook_request, excerpt = self._hook_request(request)
        result = self._delegate.delegate(hook_request)
        if result.outcome is EnumHookDelegateOutcome.DECLINED:
            return self._pass(
                request.session_id,
                EnumPromptDelegateOutcome.DECLINED,
                model=result.model,
                run_id=result.run_id,
                excerpt=excerpt,
            )
        if not result.answered:
            return self._pass(
                request.session_id,
                EnumPromptDelegateOutcome.NOT_ANSWERED,
                detail=str(result.outcome),
                excerpt=excerpt,
            )
        self._record(
            request.session_id,
            request.prompt,
            result.answer,
            result.model,
            result.run_id,
        )
        return ModelPromptDelegateResult(
            outcome=EnumPromptDelegateOutcome.ANSWERED,
            model=result.model,
            run_id=result.run_id,
            correlation_id=result.correlation_id,
            excerpt=excerpt,
            hook_output=block_output(
                result.answer, result.model, result.run_id, result.correlation_id
            ),
        )

    def _excerpt(self, transcript_path: str) -> tuple[str, EnumPromptExcerpt]:
        """The recent conversation to send, and what became of it."""
        excerpt, checked = cut_turns(transcript_turns(transcript_path))
        if not excerpt:
            return "", EnumPromptExcerpt.NONE
        if self._sensitive is None:
            return "", EnumPromptExcerpt.WITHHELD_UNCHECKED
        if self._sensitive(checked):
            return "", EnumPromptExcerpt.WITHHELD_SENSITIVE
        return excerpt, EnumPromptExcerpt.SENT

    def _hook_request(
        self, request: ModelPromptDelegateRequest
    ) -> tuple[ModelHookDelegateRequest, EnumPromptExcerpt]:
        excerpt, fate = self._excerpt(request.transcript_path)
        context = "\n\n".join(
            part
            for part in (excerpt, self._pending(request.session_id, CONTEXT_CHARS))
            if part
        )
        limit = PROMPT_TIMEOUT_S
        if request.timeout_seconds is not None:
            limit = min(limit, request.timeout_seconds)
        return ModelHookDelegateRequest(
            task=_TASK.format(context=context or "(none)", prompt=request.prompt),
            timeout_seconds=limit,
            session_id=request.session_id,
            decline_token=DECLINE_TOKEN,
            system_prompt=RULES,
        ), fate

    @classmethod
    def drain_history(cls, session_id: str) -> str:
        """Deliver accepted exchanges without classifying or delegating a prompt."""
        return context_output(cls._take_pending(session_id))

    @classmethod
    def history_hook(cls) -> str:
        """The context-only installed adapter: no classifier or model call."""
        import sys

        from omnimarket.handlers.delegation_hook_switch import delegation_switch_enabled
        from omnimarket.nodes.node_hook_delegate_effect import container

        if delegation_switch_enabled():
            return ""
        payload = json.load(sys.stdin)
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(session_id, str) or not session_id:
            return ""
        container.build_handler().begin_turn(session_id)
        return cls.drain_history(session_id)

    def _pass(
        self,
        session_id: str,
        outcome: EnumPromptDelegateOutcome,
        *,
        detail: str = "",
        model: str = "",
        run_id: str = "",
        excerpt: EnumPromptExcerpt | None = None,
    ) -> ModelPromptDelegateResult:
        return ModelPromptDelegateResult(
            outcome=outcome,
            detail=detail,
            model=model,
            run_id=run_id,
            excerpt=excerpt,
            hook_output=context_output(self._take_pending(session_id)),
        )

    # --- the per-session history of delegated exchanges -------------------

    @staticmethod
    def _history_file(session_id: str) -> Path:
        root = Path(os.environ.get("ONEX_STATE_DIR", str(Path.home() / ".onex_state")))
        name = hashlib.sha256((session_id or "no-session").encode()).hexdigest()
        return root / "claude_prompt_history" / f"{name}.json"

    @classmethod
    @contextmanager
    def _locked(cls, session_id: str) -> Iterator[tuple[Path, list[dict[str, Any]]]]:
        path = cls._history_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                entries = json.loads(path.read_text())
                if not isinstance(entries, list):
                    entries = []
            except (OSError, ValueError):
                entries = []
            yield path, entries

    @staticmethod
    def _write(path: Path, entries: list[dict[str, Any]]) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entries))
        temporary.replace(path)

    @classmethod
    def _record(
        cls, session_id: str, prompt: str, answer: str, model: str, run_id: str
    ) -> None:
        """Keep an accepted exchange for Claude; a run id already kept is ignored."""
        with cls._locked(session_id) as (path, entries):
            if run_id and any(entry.get("run_id") == run_id for entry in entries):
                return
            entries.append(
                {
                    "run_id": run_id,
                    "model": model,
                    "prompt": prompt[:ANSWER_CHARS],
                    "answer": answer[:ANSWER_CHARS],
                }
            )
            cls._write(path, entries[-HISTORY_KEEP:])

    @staticmethod
    def _render(entries: list[dict[str, Any]], limit: int) -> str:
        """Newest exchanges first until ``limit``, shown oldest to newest."""
        kept: list[str] = []
        used = 0
        for entry in reversed(entries):
            block = (
                f"USER: {entry.get('prompt', '')}\n"
                f"DELEGATED ANSWER ({entry.get('model', '')}, run {entry.get('run_id', '')}): "
                f"{entry.get('answer', '')}"
            )
            if used + len(block) > limit:
                break
            kept.append(block)
            used += len(block) + 2
        return "\n\n".join(reversed(kept))

    @classmethod
    def _pending(cls, session_id: str, limit: int) -> str:
        """Delegated exchanges Claude has not seen, without consuming them."""
        with cls._locked(session_id) as (_, entries):
            return cls._render(entries, limit)

    @classmethod
    def _take_pending(cls, session_id: str) -> str:
        """Delegated exchanges for Claude's next turn, consumed once."""
        with cls._locked(session_id) as (path, entries):
            if not entries:
                return ""
            cls._write(path, [])
            text = cls._render(entries, HISTORY_CHARS - len(_HISTORY_INTRO))
        return _HISTORY_INTRO + text if text else ""
