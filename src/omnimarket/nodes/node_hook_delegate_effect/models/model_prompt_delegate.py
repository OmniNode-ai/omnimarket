# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request and result of delegating one submitted Claude Code prompt (OMN-23413 AC33)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumPromptDelegateOutcome(StrEnum):
    """Only ANSWERED replaces Claude's turn; every other outcome passes the prompt to Claude."""

    ANSWERED = "answered"
    #: The model replied with the decline token: it cannot answer this prompt.
    DECLINED = "declined"
    EMPTY = "empty"
    SLASH_COMMAND = "slash_command"
    #: The prompt starts with "@claude".
    SKIPPED = "skipped"
    #: omniclaude's sensitivity gate kept the prompt local.
    SENSITIVE = "sensitive"
    #: The delegation ran and gave no answer; ``detail`` names the hook outcome.
    NOT_ANSWERED = "not_answered"


class ModelPromptDelegateRequest(BaseModel):
    """One prompt from Claude Code's UserPromptSubmit hook and what omniclaude knows of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(description="The prompt as the user typed it.")
    session_id: str = ""
    transcript_path: str = Field(
        default="", description="Claude Code's transcript, for the recent conversation."
    )
    intent: str = Field(
        default="", description="omniclaude's classifier intent, e.g. research."
    )
    sensitive: bool = Field(
        default=False, description="omniclaude's sensitivity gate kept it local."
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        description="The hook's own limit; a prompt never waits longer than this.",
    )

    @classmethod
    def of(
        cls,
        fields: dict[str, object],
        session_id: str,
        sensitive: bool,
        intent: str,
        timeout: object,
    ) -> ModelPromptDelegateRequest:
        """The request for a UserPromptSubmit hook payload."""
        return cls.model_validate(
            {
                "prompt": str(fields.get("prompt") or ""),
                "session_id": session_id,
                "transcript_path": str(fields.get("transcript_path") or ""),
                "intent": intent,
                "sensitive": sensitive,
                "timeout_seconds": timeout or None,
            }
        )


class ModelPromptDelegateResult(BaseModel):
    """What happened, and the hook's whole stdout for Claude Code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumPromptDelegateOutcome
    detail: str = Field(default="", description="The hook outcome when not answered.")
    model: str = ""
    run_id: str = ""
    hook_output: str = Field(
        default="",
        description=(
            'The UserPromptSubmit hook\'s stdout: {"decision": "block"} with the '
            "answer, additionalContext with delegated exchanges Claude has not "
            "seen, or nothing. Plain stdout would become Claude's context."
        ),
    )

    @property
    def log_line(self) -> str:
        why = f"{self.outcome}:{self.detail}" if self.detail else str(self.outcome)
        return f"prompt {why} model={self.model or '-'} run={self.run_id or '-'}"
