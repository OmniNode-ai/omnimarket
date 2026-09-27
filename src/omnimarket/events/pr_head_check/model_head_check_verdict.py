# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelHeadCheckVerdict: output of the ``classify_head_checks`` operation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.pr_head_check.enum_head_check_verdict import (
    HEAD_CHECK_RERUN_VERDICTS,
    EnumHeadCheckVerdict,
)
from omnimarket.events.pr_head_check.model_head_check_attempt import (
    ModelHeadCheckAttempt,
)
from omnimarket.events.pr_head_check.model_head_check_reason import (
    ModelHeadCheckReason,
)

_REPOSITORY_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_SHA_PATTERN = r"^[0-9a-f]{40}$"


class ModelHeadCheckVerdict(BaseModel):
    """The PR-level verdict for one head, and what to re-run when that is the remedy.

    ``rerun_checks`` names the checks whose runs are re-run, and is set
    exactly when the verdict's remedy is a re-run. A ``product_failed``
    verdict never names one: a real failure is fixed, not re-run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(
        ..., pattern=_REPOSITORY_PATTERN, description="owner/name of the product repo."
    )
    pr_number: int = Field(..., ge=1, description="Pull request number.")
    head_sha: str = Field(
        ..., pattern=_SHA_PATTERN, description="The head the verdict is about."
    )
    verdict: EnumHeadCheckVerdict = Field(..., description="The PR-level verdict.")
    rerun_checks: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Checks whose runs to re-run, when the remedy is a re-run.",
    )
    check_reasons: tuple[ModelHeadCheckReason, ...] = Field(
        default_factory=tuple,
        description="Per-check reason codes of the non-green required checks.",
    )
    check_attempts: tuple[ModelHeadCheckAttempt, ...] = Field(
        default_factory=tuple,
        description=(
            "The run attempt of each blocking check result the verdict read, "
            "where the facts carry one (F7). The reducer treats a result older "
            "than the attempt its last re-run started as pending."
        ),
    )

    @model_validator(mode="after")
    def _consistent(self) -> ModelHeadCheckVerdict:
        if any(not name.strip() for name in self.rerun_checks):
            raise ValueError("rerun_checks holds a blank check name")
        if len(set(self.rerun_checks)) != len(self.rerun_checks):
            raise ValueError("rerun_checks holds a duplicate check name")
        if self.verdict is EnumHeadCheckVerdict.PRODUCT_FAILED and self.rerun_checks:
            raise ValueError(
                "a product_failed verdict names no re-runnable check "
                f"(rerun_checks={list(self.rerun_checks)}): a real failure is fixed, "
                "not re-run"
            )
        if self.verdict in HEAD_CHECK_RERUN_VERDICTS and not self.rerun_checks:
            raise ValueError(
                f"a {self.verdict} verdict re-runs, so rerun_checks must name the checks"
            )
        if self.verdict not in HEAD_CHECK_RERUN_VERDICTS and self.rerun_checks:
            raise ValueError(
                f"a {self.verdict} verdict does not re-run, so rerun_checks must be empty"
            )
        names = [reason.name for reason in self.check_reasons]
        if len(set(names)) != len(names):
            raise ValueError("check_reasons holds a duplicate check name")
        attempted = [attempt.check for attempt in self.check_attempts]
        if len(set(attempted)) != len(attempted):
            raise ValueError("check_attempts holds a duplicate check name")
        return self


__all__: list[str] = ["ModelHeadCheckVerdict"]
