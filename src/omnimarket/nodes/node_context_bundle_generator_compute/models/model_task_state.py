"""Task state model for context bundle generation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumTaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class EnumTaskPriority(StrEnum):
    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NO_PRIORITY = "no_priority"


class ModelTaskState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(min_length=1)
    title: str = ""
    status: EnumTaskStatus = EnumTaskStatus.TODO
    assignee: str = ""
    priority: EnumTaskPriority = EnumTaskPriority.NO_PRIORITY
    labels: tuple[str, ...] = ()
    parent_ticket_id: str = ""
    related_ticket_ids: tuple[str, ...] = ()


__all__ = ["EnumTaskPriority", "EnumTaskStatus", "ModelTaskState"]
