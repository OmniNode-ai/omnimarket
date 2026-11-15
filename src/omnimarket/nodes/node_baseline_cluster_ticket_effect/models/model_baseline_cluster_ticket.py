# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the dev-baseline cluster ticket effect: the report request, its result, and the bus models."""

from __future__ import annotations

from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

OVERLAY_PREFIX = "baseline_cluster."
OVERLAY_KEYS = (
    "parent",
    "gate",
    "diagnosis",
    "timeout_seconds",
    "max_duplicate_hops",
    "example_limit",
)


def _refuse_missing_overlay_keys(data: Any) -> Any:
    if isinstance(data, dict):
        missing = [key for key in OVERLAY_KEYS if data.get(key) is None]
        if missing:
            raise ValueError(
                "no overlay supplies "
                + ", ".join(f"{OVERLAY_PREFIX}{k}" for k in missing)
            )
    return data


class ModelBaselineClusterReportRequest(BaseModel):
    """One dev-baseline cluster report: what failed, and the overlay-carried policy of its ticket effect.

    Every store call of the report runs under ``baseline_cluster.timeout_seconds``; the
    other overlay keys steer the search, the duplicate chain and the description.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    failing_count: int = Field(ge=1)
    examples: tuple[str, ...]
    run_url: str
    observed_at: str = Field(min_length=1)
    requested_by_lane: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    # baseline_cluster.parent: no default, a request that lacks it is refused naming the key.
    parent: str = Field(pattern=r"^OMN-[0-9]+$")
    # baseline_cluster.gate: no default, a request that lacks it is refused naming the key.
    gate: str = Field(min_length=1)
    # baseline_cluster.diagnosis: no default, a request that lacks it is refused naming the key.
    diagnosis: str = Field(min_length=1)
    # baseline_cluster.timeout_seconds: no default, a request that lacks it is refused naming the key.
    timeout_seconds: float = Field(gt=0)
    # baseline_cluster.max_duplicate_hops: no default, a request that lacks it is refused naming the key.
    max_duplicate_hops: int = Field(ge=1)
    # baseline_cluster.example_limit: no default, a request that lacks it is refused naming the key.
    example_limit: int = Field(ge=1)

    @model_validator(mode="before")
    @classmethod
    def _keys_are_supplied(cls, data: Any) -> Any:
        return _refuse_missing_overlay_keys(data)


class ModelBaselineClusterReportResult(BaseModel):
    """The effect of one cluster report: a new ticket, a comment on a found one, or no answer this run.

    ``cause`` of a refused report reads ``ticket-refused:<cause>``; of an unavailable report it
    names the call that failed or ran past the deadline (``search-failed:<cause>``,
    ``search-timeout``, ``read-failed:<cause>``, ``read-timeout``, ``comment-failed:<cause>``,
    ``create-failed:<cause>``). ``via`` names the closed duplicate a linked comment went through;
    ``previous_ticket`` names the closed ticket the created one continues.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["created", "commented", "linked", "refused", "unavailable"]
    ticket: str | None = None
    via: str | None = None
    previous_ticket: str | None = None
    cause: str | None = None

    @model_validator(mode="after")
    def _shape_follows_state(self) -> Self:
        if self.state in ("created", "commented", "linked"):
            if not self.ticket or self.cause is not None:
                raise ValueError(
                    "a created, commented or linked result names the ticket and no cause"
                )
        elif self.ticket is not None or not self.cause:
            raise ValueError(f"{self.state} names a cause and no ticket")
        if self.via is not None and self.state != "linked":
            raise ValueError("only a linked result carries via")
        if self.previous_ticket is not None and self.state != "created":
            raise ValueError("only a created result carries previous_ticket")
        return self


class ModelBaselineClusterReportCommand(BaseModel):
    """One bus command: one cluster report and its caller identity.

    It carries no overlay values: the parent, gate, deadline and limits are the overlay's
    ``baseline_cluster`` section, which the bus handler reads when it handles the command.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    requested_by_lane: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    repo: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    failing_count: int = Field(ge=1)
    examples: tuple[str, ...]
    run_url: str
    observed_at: str = Field(min_length=1)


class ModelBaselineClusterReportCompleted(BaseModel):
    """The completed-topic answer of a bus cluster report: the same result, correlated by the command's id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    requested_by_lane: str
    repo: str
    cluster: str
    outcome: Literal["completed"] = "completed"
    result: ModelBaselineClusterReportResult


class ModelBaselineClusterBusOverlay(BaseModel):
    """The bus section of the node overlay: the deadline of each store call and the store the bus binds.

    No defaults: a missing key refuses naming it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: float = Field(gt=0)
    ticket_store: Literal["inmemory", "linear"]
    parent: str = Field(pattern=r"^OMN-[0-9]+$")
    gate: str = Field(min_length=1)
    diagnosis: str = Field(min_length=1)
    max_duplicate_hops: int = Field(ge=1)
    example_limit: int = Field(ge=1)


class ModelBaselineClusterOverlayDocument(BaseModel):
    """The node's overlay document: ``baseline_cluster`` with the bus section's keys.

    The document also carries ``event_bus`` (the bus ``onex run-node`` dials, read by the
    broker resolver), which this model ignores; inside ``baseline_cluster`` an unknown key is
    refused.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    baseline_cluster: ModelBaselineClusterBusOverlay
