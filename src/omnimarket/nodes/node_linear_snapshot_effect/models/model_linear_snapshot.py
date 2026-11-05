# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Linear snapshot ``linear-<UTC date>.json`` and the request and result of the node that writes it.

One typed shape serves every reader of the file: the lab fill generator (``id``, ``title``,
``description``, ``statusType``, ``assignee``, ``labels``, ``project`` per issue and the top-level
``operator``), plans-today's identity sidecar (``operator`` is the resolved Linear user id of the
signed-in subject, never the literal ``me``, and every issue names its ``assignee``) and plans-today's
snapshot loader (``status``, ``statusType``, ``updatedAt``, ``priority``). The snapshot holds the signed-in
subject's open issues, so every issue's ``assignee`` is the ``operator``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from omnimarket.models.model_linear_snapshot_request import (
    ModelLinearSnapshotRequest as ModelLinearSnapshotRequest,
)

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TicketId = Annotated[str, StringConstraints(pattern=r"^OMN-\d+$")]
SNAPSHOT_SCHEMA = "omni.linear-snapshot/1"


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
    updated_at: NonEmpty = Field(alias="updatedAt")
    description: str = ""
    priority: str | None = None


class ModelLinearSnapshot(BaseModel):
    """The file: who it was read as, and that subject's open issues."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    operator: NonEmpty
    issues: tuple[ModelSnapshotIssue, ...]
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

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


class ModelLinearSnapshotResult(BaseModel):
    """What the write did: counts only, never ticket text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["written", "failed"]
    path: str
    issue_count: int = Field(ge=0)
    with_description: int = Field(ge=0)
    reason: str = ""
