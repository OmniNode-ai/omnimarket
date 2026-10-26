"""Typed inputs and outputs of the controller repair plan (OMN-19998)."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelControllerFixPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["target", "brief", "lanes"]
    contract: dict[str, Any]
    controller_host: str | None = None
    lab_headroom: dict[str, Any] | None = None
    defects: list[dict[str, Any]] = Field(default_factory=list)
    history: dict[str, list[str]] = Field(default_factory=dict)
    lane: str = ""
    bound_min: float = 30
    # operation lanes (OMN-19998): the tick's clock, its per-head history, each defect's ``key@head``, and per lane
    # its newest runner receipt (started, final, status) and its ledger rows (claim, terminal row text or "")
    now: str = ""
    dispatched: dict[str, Any] = Field(default_factory=dict)
    idents: dict[str, str] = Field(default_factory=dict)
    receipts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    ledger_lanes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    live_min: float = 90


class ModelControllerFixPlanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: dict[str, Any] = Field(default_factory=dict)
    host: str | None = None
    error: str = ""
    brief: str = ""
    live: str | None = None
    dead: list[str] = Field(default_factory=list)
    resend: list[str] = Field(default_factory=list)
