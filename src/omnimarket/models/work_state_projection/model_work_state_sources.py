# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the projection reads, one typed row per source item. Built from the sources, never from the floor page."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelTicketSource(BaseModel):
    """One tracker ticket: its state type, project and the references its start criterion requires."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    title: str = ""
    state_type: str
    project: str = ""
    # The tracker's id of the project, where the source carries one: scope matches a project by name or by id.
    project_id: str = ""
    parent: str | None = None
    requires: tuple[str, ...] = ()


class ModelAncestorSource(BaseModel):
    """One ancestor the Linear snapshot names but does not list as an open issue: only its parent and project, for the
    scope walk to read. It is never a work item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    parent: str | None = None
    project: str = ""
    project_id: str = ""


class ModelLinearRow(BaseModel):
    """One row of the Linear source: an open issue, or an ancestor that only the scope walk reads. Exactly one is set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: ModelTicketSource | None = None
    ancestor: ModelAncestorSource | None = None


class ModelPlanTaskSource(BaseModel):
    """One task of a plan file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan: str
    task_id: str
    title: str = ""
    ticket: str | None = None
    done: bool = False
    requires: tuple[str, ...] = ()


class ModelPullRequestSource(BaseModel):
    """One pull request from the PR watcher, with the landing controller's decision and its red required checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    number: int
    title: str = ""
    ticket: str | None = None
    # The ticket a `Ticket: <id>` line of the body names, where the title names none.
    body_ticket: str | None = None
    author: str = ""
    head_sha: str = ""
    state: str
    draft: bool = False
    controller_decision: str | None = None
    red_required_checks: tuple[str, ...] = ()
    # A red check's cause key when the landing controller has named one, by check name.
    cause_keys: dict[str, str] = {}


class ModelReadingSource(BaseModel):
    """One piece or host reading: a key and its current value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    value: str
    read_at: datetime | None = None


class ModelLaneReceiptSource(BaseModel):
    """One lane receipt from the lane runner: whether the engine is still running."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    status: str
    exit_code: int | None = None


class ModelCommitmentSource(BaseModel):
    """One operator ask recorded as a ledger commitment, folded by node_commitment_ledger_compute.

    `status` is open (no reading yet), verified (its check passes) or regressed (its check failed after it passed).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    commitment_id: str
    ask: str
    owner: str
    check: str
    status: str
    opened_at: datetime
    ticket: str | None = None
    requires: tuple[str, ...] = ()
    last_checked_at: datetime | None = None
    last_exit: int | None = None


__all__ = [
    "ModelAncestorSource",
    "ModelCommitmentSource",
    "ModelLaneReceiptSource",
    "ModelLinearRow",
    "ModelPlanTaskSource",
    "ModelPullRequestSource",
    "ModelReadingSource",
    "ModelTicketSource",
]
