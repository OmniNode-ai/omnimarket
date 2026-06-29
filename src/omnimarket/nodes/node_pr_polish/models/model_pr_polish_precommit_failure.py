"""ModelPrPolishPrecommitFailure — structured signal for a failed pre-commit run.

When the live PR polish workflow runs ``pre-commit run --all-files`` in the PR
worktree and it exits non-zero, the failing hook ids and reported file paths are
the durable signal a downstream classifier needs to route repo-baseline debt
away from PR-scoped polish. This model captures that signal so it can ride the
``onex.evt.omnimarket.pr-polish-completed.v1`` event instead of being collapsed
into a single error-message line.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelPrPolishPrecommitFailure(BaseModel):
    """Structured pre-commit failure signal attached to the completed event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str = Field(
        ...,
        description="The pre-commit command string that exited non-zero.",
    )
    exit_code: int = Field(
        ...,
        description="The non-zero exit code returned by the pre-commit command.",
    )
    hook_ids: tuple[str, ...] = Field(
        default=(),
        description="Failing hook ids parsed from the pre-commit output.",
    )
    paths: tuple[str, ...] = Field(
        default=(),
        description="File paths the failing hooks reported, parsed from output.",
    )
    output_tail: str = Field(
        default="",
        description="Bounded tail of the captured pre-commit stdout/stderr.",
    )


__all__: list[str] = ["ModelPrPolishPrecommitFailure"]
