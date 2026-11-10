# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Linear snapshot ``linear-<UTC date>.json`` and the request and result of the node that writes it.

One typed shape serves every reader of the file: the lab fill generator (``id``, ``title``,
``description``, ``statusType``, ``assignee``, ``labels``, ``project``, ``projectId``, ``parentId`` per issue and the
top-level ``operator``), plans-today's identity sidecar (``operator`` is the resolved Linear user id of the
signed-in subject, never the literal ``me``, and every issue names its ``assignee``) and plans-today's
snapshot loader (``status``, ``statusType``, ``updatedAt``, ``priority``). The snapshot holds the signed-in
subject's open issues, so every issue's ``assignee`` is the ``operator``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TicketId = Annotated[str, StringConstraints(pattern=r"^OMN-\d+$")]
SNAPSHOT_SCHEMA = "omni.linear-snapshot/1"


class ModelSnapshotAttachment(BaseModel):
    """One attachment of an issue (a pull request link, for the merged-implementation rule), as Linear returns it."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    source_type: str = Field(default="", alias="sourceType")
    title: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelSnapshotIssue(BaseModel):
    """One issue of the snapshot, as the JSON file spells it (``statusType`` and ``updatedAt`` are camel case)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    id: TicketId
    title: str
    status: str
    status_type: str = Field(alias="statusType")
    assignee: NonEmpty
    labels: tuple[str, ...] = ()
    project: str = ""
    # The project's id, so a reader can match a project by id as well as by name; empty for an issue in no project and
    # in a file written before the key existed.
    project_id: str = Field(default="", alias="projectId")
    # The identifier of the issue's parent (``OMN-22867``), null for an issue with none and in a file written before the
    # key existed. The work list reads scope up this chain.
    parent_id: str | None = Field(default=None, alias="parentId")
    updated_at: NonEmpty = Field(alias="updatedAt")
    description: str = ""
    priority: str | None = None
    #: The issue's attachments and its child ids, as the read saw them. ``None`` means the part was not read (a file
    #: written before the node held it, or a page with more than the read asked for), never "has none": a reader that
    #: needs the part reads Linear for it. ``()`` means read and empty.
    attachments: tuple[ModelSnapshotAttachment, ...] | None = None
    children: tuple[str, ...] | None = None


class ModelSnapshotAncestor(BaseModel):
    """One ancestor of an issue that is not itself in ``issues`` (unassigned to the operator, or closed): the parent and
    project the work list's scope walk reads. It is not an open issue and never a work item."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    id: NonEmpty
    parent_id: str | None = Field(default=None, alias="parentId")
    project: str = ""
    project_id: str = Field(default="", alias="projectId")


class ModelLinearSnapshot(BaseModel):
    """The file: who it was read as, that subject's open issues and the ancestors those issues name."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    operator: NonEmpty
    issues: tuple[ModelSnapshotIssue, ...]
    # Every ancestor of an issue that is not itself an issue, once; absent in a file written before the key existed.
    ancestors: tuple[ModelSnapshotAncestor, ...] = ()

    @model_validator(mode="after")
    def _read_as_a_resolved_subject(self) -> ModelLinearSnapshot:
        if self.operator.lower() == "me":
            raise ValueError(
                "operator is the literal 'me', not the resolved Linear user id of the signed-in subject"
            )
        foreign = sorted(i.id for i in self.issues if i.assignee != self.operator)
        if foreign:
            raise ValueError(
                f"issues not assigned to the operator: {', '.join(foreign)}"
            )
        return self


class ModelLinearSnapshotRequest(BaseModel):
    """One write: the directory to write into and the UTC date in the file name (``None``: today)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    out_dir: NonEmpty
    date: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = (
        None
    )
    page_size: int = Field(default=100, ge=1, le=250)
    max_pages: int = Field(default=60, ge=1)
    #: Longest total wait on Linear's rate limit in one run; a Retry-After past what is left ends the wait at once.
    backoff_budget_s: float = Field(default=1500.0, ge=0)
    #: A refused read keeps the last good snapshot and exits 0 while it is younger than this; older is a failure.
    max_stale_s: int = Field(default=21600, ge=0)
    #: The open list is rebuilt by a full read once this old, so a deleted or archived issue cannot linger.
    full_read_every_s: int = Field(default=86400, ge=1)
    #: Each incremental read starts this far before the last good read, so an update racing it is read twice, never missed.
    overlap_s: int = Field(default=600, ge=0)
    #: How many levels of ``parent`` each issue's query selects, so a chain through an unassigned or closed ancestor reads.
    ancestor_depth: int = Field(default=6, ge=0, le=8)


class ModelLinearSnapshotResult(BaseModel):
    """What the write did: counts only, never ticket text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["written", "stale", "failed"]
    path: str
    issue_count: int = Field(ge=0)
    with_description: int = Field(ge=0)
    reason: str = ""
    #: ``full`` or ``incremental`` for a write; empty when nothing was read.
    mode: str = ""
    #: Seconds since the last good read, set on ``stale`` (the kept snapshot's age) and 0 on ``written``.
    age_s: int | None = Field(default=None, ge=0)


class ModelLinearSnapshotState(BaseModel):
    """The sidecar beside the snapshot: when it was last read well, and what the last attempt did."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_id: Literal["omni.linear-snapshot-state/1"] = Field(
        default="omni.linear-snapshot-state/1", alias="schema"
    )
    last_good_read_at: NonEmpty
    last_full_read_at: NonEmpty
    last_attempt_at: NonEmpty
    last_attempt_status: Literal["written", "stale", "failed"]
    last_attempt_reason: str = ""
    age_s: int = Field(default=0, ge=0)
