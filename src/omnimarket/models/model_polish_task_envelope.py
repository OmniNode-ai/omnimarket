from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_polish_task_class import EnumPolishTaskClass


class ModelPolishTaskEnvelope(BaseModel):
    """Per-PR polish task envelope. Routing policy slot reserved for Phase 2."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    task_class: EnumPolishTaskClass
    pr_number: int
    repo: str
    correlation_id: UUID
    # Phase 2 will bind a ModelRoutingPolicy here; Phase 1 leaves it None.
    routing_policy: dict[str, Any] | None = Field(default=None)
