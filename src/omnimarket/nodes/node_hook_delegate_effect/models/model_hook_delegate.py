# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request, the result and the CLI run of the hook delegate effect."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumHookDelegateOutcome(StrEnum):
    """Only ANSWERED blocks the subagent; every other outcome passes it through."""

    ANSWERED = "answered"
    GATEWAY_OWNS_SESSION = "gateway_owns_session"
    NO_ONEX = "no_onex"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    NO_ANSWER = "no_answer"
    #: The model replied with the request's decline token (OMN-23413 AC33).
    DECLINED = "declined"


class EnumDelegateCliState(StrEnum):
    """How one `onex delegate` process ended."""

    EXITED = "exited"
    TIMED_OUT = "timed_out"
    NOT_STARTED = "not_started"
    NOT_FOUND = "not_found"


class ModelHookDelegateRequest(BaseModel):
    """One subagent task, the intent the hook's classifier gave it, and its time limit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str = Field(min_length=1, description="The subagent's prompt.")
    intent: str = Field(
        default="", description="The classifier's intent, e.g. research."
    )
    timeout_seconds: float = Field(
        gt=0, description="How long `onex delegate` may take."
    )
    session_id: str = ""
    decline_token: str = Field(
        default="",
        description=(
            "When set, a reply that declares this token ('token:' anywhere, or the "
            "token alone on a line) is DECLINED, even when onex delegate's quality "
            "gates vetoed it (OMN-23413 AC33)."
        ),
    )
    # OMN-23413 AC33: a prompt states its own definition of done. Graded by the
    # research rubric, short declines were vetoed and retried down the ladder and
    # one-sentence answers ran to thousands of tokens. Empty means the task
    # class's own default, as before.
    system_prompt: str = Field(default="", description="onex delegate --system-prompt.")
    criteria: tuple[str, ...] = Field(
        default=(), description="onex delegate --criteria slugs, in order."
    )
    criteria_mode: str = Field(
        default="", description="onex delegate --criteria-mode, with criteria."
    )
    max_tokens: int | None = Field(
        default=None, ge=1, description="onex delegate --max-tokens."
    )

    @classmethod
    def of(
        cls, tool_input: object, tool_text: str, intent: object, timeout: object
    ) -> ModelHookDelegateRequest:
        """The Agent or Task call's prompt, else its whole input as text."""
        prompt = tool_input.get("prompt") if isinstance(tool_input, dict) else None
        task = prompt if isinstance(prompt, str) and prompt.strip() else tool_text
        return cls.model_validate(
            {"task": task, "intent": str(intent or ""), "timeout_seconds": timeout}
        )


class ModelDelegateCliRun(BaseModel):
    """What one `onex delegate` process did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: EnumDelegateCliState
    exit_code: int | None = None
    stdout: str = ""


class ModelHookDelegateResult(BaseModel):
    """The model's answer when one came back; otherwise why the task passes through."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumHookDelegateOutcome
    answer: str = Field(default="", description="The delegated answer.")
    model: str = Field(default="", description="The model that answered.")
    run_id: str = Field(default="", description="The onex delegate run id.")

    @property
    def answered(self) -> bool:
        return self.outcome is EnumHookDelegateOutcome.ANSWERED
