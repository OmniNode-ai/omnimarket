"""Phase receipts and the discovery workflow's public result."""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictInt,
    model_serializer,
)

from .model_discovery_request import ModelDiscoveryProfile


def require_delegation(lane: str, result: Mapping[str, object]) -> None:
    """Preserve OMN-17009's receipt/outage admission rules at each boundary."""
    cell = result.get("delegation")
    problem = ""
    if not isinstance(cell, dict):
        problem = "missing or invalid cell"
    else:
        count, runs, reason = (
            cell.get("delegated"),
            cell.get("runs"),
            cell.get("reason"),
        )
        if type(count) is not int or count < 0:
            problem = "invalid delegated count"
        elif not isinstance(runs, list) or not all(
            isinstance(run, str) and re.fullmatch(r"\S+", run) for run in runs
        ):
            problem = "invalid run ids"
        elif not isinstance(reason, str):
            problem = "missing or invalid reason"
        elif count > 0:
            if not runs:
                problem = "delegated steps have no runs"
            elif reason != "":
                problem = "delegated steps require an empty reason"
        elif runs:
            problem = "zero delegation has runs"
        elif not re.fullmatch(r"route-(?:refused|unavailable):\S+", reason):
            problem = "zero delegation reason refused"
    if problem:
        raise ValueError(f"delegation cell refused (OMN-17009): {lane}: {problem}")


class ModelDiscoveryDelegation(BaseModel):
    """The phase's receipted drafting work or recorded route outage."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    delegated: StrictInt
    runs: tuple[str, ...]
    reason: str


class ModelDiscoveryPhaseResult(BaseModel):
    """Fields from the three phase schemas; the selected schema admits a result."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    delegation: ModelDiscoveryDelegation
    ledger_rows: tuple[str, ...]
    handoff_path: str | None = None
    sources_read: tuple[str, ...] | None = None
    item_count: float | None = None
    unreadable_sources: tuple[str, ...] | None = None
    unintegrated: tuple[str, ...] | None = None
    non_beta_backlog: tuple[str, ...] | None = None
    automation_candidates: tuple[str, ...] | None = None
    slate: tuple[str, ...] | None = None
    excluded: tuple[str, ...] | None = None

    @model_serializer(mode="wrap")
    def omit_absent_fields(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        return {key: value for key, value in handler(self).items() if value is not None}

    report_path: str | None = None
    state_path: str | None = None
    commit_sha: str | None = None
    slate_summary: str | None = None


class ModelDiscoveryResult(BaseModel):
    """Completed full discovery; no hidden watermark or implicit assignee."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    date: str
    write_state: bool = Field(alias="writeState")
    profiles: tuple[ModelDiscoveryProfile, ...]
    report: str
    delegation: ModelDiscoveryDelegation
    state: str
    commit: str
    slate: str
    scan: dict[str, ModelDiscoveryPhaseResult]
    adjudication: ModelDiscoveryPhaseResult

    def legacy_payload(self) -> dict[str, object]:
        """Keep absent phase fields absent in the public JSON, as in the workflow."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)
