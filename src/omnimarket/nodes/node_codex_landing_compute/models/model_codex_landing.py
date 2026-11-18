# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed commands, hook comparisons and handoff decisions for Codex landing."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

HookStage = Literal["pre-commit", "pre-push"]
HookClass = Literal["ENV", "CODE"]
HookDisposition = Literal["base-inherited", "fixable", "blocked"]


@dataclass(frozen=True)
class ModelCodexHookPlan:
    hooks: tuple[str, ...]
    files: tuple[str, ...]
    stage: HookStage = "pre-commit"


@dataclass(frozen=True)
class ModelCodexHookRun:
    hook: str
    returncode: int | None
    output: str
    stage: HookStage = "pre-commit"
    rewrote_files: bool = False
    worktree: str = ""


@dataclass(frozen=True)
class ModelCodexHookComparison:
    """Observations from the staged change and clean base with identical argv and environment."""

    files: tuple[str, ...]
    lane: tuple[ModelCodexHookRun, ...]
    base: tuple[ModelCodexHookRun, ...]


@dataclass(frozen=True)
class ModelCodexHookFailure:
    hook: str
    stage: HookStage
    first_line: str
    classification: HookDisposition
    hook_class: HookClass
    about_change: bool


@dataclass(frozen=True)
class ModelCodexHookDecision:
    may_commit: bool
    failures: tuple[ModelCodexHookFailure, ...]


@dataclass(frozen=True)
class ModelCodexHandoffRequest:
    """The install-proof node's verdict using the final report as its sole proof source."""

    pr: str
    head: str
    report: str
    install_verdict: Literal["OK", "NA", "NO", "REFUSED"]
    install_detail: str = ""


@dataclass(frozen=True)
class ModelCodexProofNotice:
    pr: str
    head: str
    missing_proof: str


@dataclass(frozen=True)
class ModelCodexHandoffDecision:
    handoff: bool
    notice: ModelCodexProofNotice | None = None


class ModelCodexLandingRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    worktree: str
    hook_plan: ModelCodexHookPlan | None = None
    hook_comparison: ModelCodexHookComparison | None = None
    handoff: ModelCodexHandoffRequest | None = None

    def __init__(self, worktree: str, **data: object) -> None:
        super().__init__(worktree=worktree, **data)


class ModelCodexLandingResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clean_command: str
    json_reader: str
    hook_argv: tuple[tuple[str, ...], ...] | None = None
    hook_decision: ModelCodexHookDecision | None = None
    handoff_decision: ModelCodexHandoffDecision | None = None

    def __init__(self, clean_command: str, json_reader: str, **data: object) -> None:
        super().__init__(clean_command=clean_command, json_reader=json_reader, **data)
