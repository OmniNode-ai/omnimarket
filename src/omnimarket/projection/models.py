"""Pydantic models for the contract-driven projection API.

These models represent the discovered configuration for each exposed projection
topic. All fields are read from contract.yaml — no convention-based defaults
for topics, columns, or ordering.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ProjectionStatus(StrEnum):
    """Lifecycle status of a discovered projection topic."""

    OK = "ok"
    DEGRADED = "degraded"


class ProjectionTableConfig(BaseModel):
    """Configuration for a single projection topic, read from contract.

    All query parameters come from the ``projection_api`` section of the node's
    contract.yaml. None are inferred from column names, directory names, or
    database introspection.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    table: str
    schema_name: str = "public"
    # tuple[str, ...] for declared columns; tuple[Literal["*"]] for SELECT *
    columns: tuple[str, ...] | tuple[Literal["*"]]
    order_by: str | None = None  # None means ordering is undefined
    freshness_column: str | None = None  # None means freshness is unknown
    limit: int = 100
    source_contract: str = ""  # node name for tracing
    status: ProjectionStatus = ProjectionStatus.OK
    degraded_reason: str = ""
