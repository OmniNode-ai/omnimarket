# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plans-today request and result, and the Linear sidecar a member run reads.

The run's mode is never an argument. Every invocation resolves the caller through the team identity
declaration (``node_team_identity_compute``) from the Linear subject recorded in the sidecar beside the
Linear snapshot. Only an authenticated operator match runs the operator's default mode; anything else
runs member mode or refuses, never the operator's mode.

The sidecar is ``<snapshot stem>.json`` beside the ``.txt`` snapshot (the snapshot itself when it is
the ``.json``). Its ``subject`` is the Linear user id of the signed-in subject (the legacy ``operator``
key is read when ``subject`` is absent; the literal ``me`` is refused). Each issue names its ``assignee``
and the GitHub pull request URLs Linear attaches to it; ``pull_requests`` ``null`` means the attachments
could not be read.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TicketId = Annotated[str, StringConstraints(pattern=r"^OMN-\d+$")]
PlansTodayMode = Literal["operator", "member"]
SummaryMode = Literal["top", "all", "none"]
PrSource = Literal["pr-watcher", "gh-pr-view"]


class ModelLinearSidecarIssue(BaseModel):
    """One issue of the sidecar's assigned-issue read."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: TicketId
    assignee: NonEmpty
    status: str = ""
    title: str = ""
    # ``None``: the attachments of this issue could not be read.
    pull_requests: tuple[NonEmpty, ...] | None = None


class ModelLinearSidecar(BaseModel):
    """``linear-<UTC date>.json``: who the snapshot was read as, and that subject's issues."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    subject: NonEmpty
    issues: tuple[ModelLinearSidecarIssue, ...]

    @model_validator(mode="before")
    @classmethod
    def _legacy_operator_key(cls, data: Any) -> Any:
        if isinstance(data, dict) and "subject" not in data and "operator" in data:
            return {**data, "subject": data["operator"]}
        return data

    @model_validator(mode="after")
    def _resolved_subject(self) -> ModelLinearSidecar:
        if self.subject.lower() == "me":
            raise ValueError(
                "subject is the literal 'me', not the resolved Linear user id of the signed-in subject"
            )
        return self


class ModelPlansTodayRequest(BaseModel):
    """One plans-today run; the fields are the script's flags. ``None`` paths take their defaults.

    ``overlay_roots`` ``None`` reads ``ONEX_SKILL_OVERLAY_ROOTS``; ``plugin_root`` ``None`` reads
    ``CLAUDE_PLUGIN_ROOT`` (the ledger reader, the MSG executor and the GLM harness live there);
    ``kb`` ``None`` reads ``ONEX_PLANS_KB_PATH``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    json_only: bool = False
    kb: str | None = None
    no_fetch: bool = False
    watcher: str | None = None
    max_watcher_age_min: int = 90
    linear_snapshot: str | None = None
    no_linear: bool = False
    ledger_rows_file: str | None = None
    window_days: int = 14
    plan: tuple[str, ...] = ()
    top: int = 12
    all: bool = False
    summaries: SummaryMode = "top"
    summary_limit: int = 8
    summary_timeout: int = 240
    summaries_budget: int | None = None
    workers: int = 8
    out_dir: str | None = None
    no_msg: bool = False
    orchestrator: str = "orchestrator"
    lane: str = "plans-today"
    now: str | None = None
    home: str | None = None  # the session home; None reads OMNI_HOME
    overlay_roots: tuple[str, ...] | None = None
    plugin_root: str | None = None

    @model_validator(mode="after")
    def _one_linear_choice(self) -> ModelPlansTodayRequest:
        if self.linear_snapshot is not None and self.no_linear:
            raise ValueError("linear_snapshot and no_linear are mutually exclusive")
        return self


class ModelPlansTodayResult(BaseModel):
    """The run's exit code (0 ok, 2 refused, 4 a MSG failed), what it printed, and its JSON."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    mode: PlansTodayMode | None
    refused: str | None
    stdout: str
    document: dict[str, Any] | None = Field(default=None)
