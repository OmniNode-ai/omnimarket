"""One contract-selected discovery phase."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue


class ModelDiscoveryTask(BaseModel):
    """A phase prompt and its response schema for the existing invocation effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str
    phase: Literal["Scan", "Adjudicate", "Report"]
    model: str
    effort: str
    prompt: str
    schema_definition: dict[str, JsonValue]
    workspace_path: str
    correlation_id: UUID
    timeout_ms: int
