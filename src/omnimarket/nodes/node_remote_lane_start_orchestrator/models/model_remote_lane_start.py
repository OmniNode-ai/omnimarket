# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane start request, its overlay settings, its receipt and its result (OMN-22848)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class EnumRemoteLaneStartOutcome(StrEnum):
    """How one lane start ended, as the leader sees it (the ledger decides what happened)."""

    UNSUPPORTED_SHAPE = "unsupported-shape"
    NO_HOST = "no-host"
    CLAIM_REFUSED = "claim-refused"
    ALREADY_CLAIMED = "already-claimed"
    START_FAILED = "start-failed"
    START_UNRESOLVED = "start-unresolved"
    ENDED = "ended"
    STOPPED = "stopped"
    STOP_REFUSED = "stop-refused"


#: The leader's exit code per outcome; ``ended`` passes the close decision through instead.
EXIT_CODES: dict[EnumRemoteLaneStartOutcome, int] = {
    EnumRemoteLaneStartOutcome.UNSUPPORTED_SHAPE: 64,
    EnumRemoteLaneStartOutcome.NO_HOST: 75,
    EnumRemoteLaneStartOutcome.CLAIM_REFUSED: 70,
    EnumRemoteLaneStartOutcome.ALREADY_CLAIMED: 70,
    EnumRemoteLaneStartOutcome.START_FAILED: 1,
    EnumRemoteLaneStartOutcome.START_UNRESOLVED: 1,
    EnumRemoteLaneStartOutcome.STOPPED: 143,
    EnumRemoteLaneStartOutcome.STOP_REFUSED: 143,
}


class ModelRemoteLaneStartSettings(BaseModel):
    """The ``remote_lane_start`` overlay section. Every key is required: a missing one refuses naming it.

    The frozen script's constants and environment reads for the lab Claude ``run`` path, each
    now a key here (the PR body's table maps each one to its script line).
    """

    model_config = _FROZEN

    supported_engines: tuple[str, ...] = Field(min_length=1)
    supported_placements: tuple[str, ...] = Field(min_length=1)
    engine_argv: tuple[str, ...] = Field(min_length=1)
    engine_environment: dict[str, str]
    start_timeout_seconds: float = Field(gt=0)
    prepare_timeout_seconds: float = Field(gt=0)
    host_call_timeout_seconds: float = Field(gt=0)
    poll_interval_seconds: float = Field(ge=0)
    stop_grace_seconds: float = Field(gt=0)
    receipt_dir: str = Field(min_length=1)
    host_run_root: str = Field(min_length=1)
    registry_root_candidates: tuple[str, ...]
    ssh_argv: tuple[str, ...]
    detach_argv: tuple[str, ...] = Field(min_length=1)
    ledger_append_argv: tuple[str, ...] = Field(min_length=1)
    ledger_writer_principal: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    displaces: str = Field(min_length=1)
    est_hours: int = Field(ge=1)
    priority: str = Field(min_length=1)
    claim_text: str = Field(min_length=1)

    @classmethod
    def from_overlay(cls, overlay: dict[str, Any]) -> ModelRemoteLaneStartSettings:
        """Read the ``remote_lane_start`` section; a missing key raises naming ``remote_lane_start.<key>``."""
        section = overlay.get("remote_lane_start")
        if not isinstance(section, dict):
            raise KeyError("remote_lane_start")
        missing = [name for name in cls.model_fields if name not in section]
        if missing:
            raise KeyError(", ".join(f"remote_lane_start.{name}" for name in missing))
        try:
            return cls.model_validate(section)
        except ValidationError as exc:
            raise ValueError(f"remote_lane_start: {exc}") from exc


class ModelRemoteLaneStartRequest(BaseModel):
    """One lane to start: its name, the fill's key, its ticket, engine, placement and ledger."""

    model_config = _FROZEN

    lane: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    key: str = Field(min_length=1, pattern=r"^[^|\s]+$")
    ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    parent: str | None = None
    engine: str = Field(min_length=1)
    placement: str = Field(min_length=1)
    model: str = Field(min_length=1)
    ledger_path: str = Field(min_length=1)
    brief: str = Field(min_length=1)
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    attempt: int = Field(ge=1)
    pinned_host: str | None = None
    escalation_reason: str | None = None
    repo: str | None = None
    owner: str | None = None
    sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class ModelRemoteLaneStartReceipt(BaseModel):
    """The lane's receipt file, rewritten at each state change."""

    model_config = ConfigDict(extra="forbid")

    lane: str
    run_id: str
    key: str
    ticket: str
    state: str
    host: str | None = None
    claim_request_id: str | None = None
    claim_row: str | None = None
    session_id: str | None = None
    workdir: str | None = None
    log_lines: int = 0
    engine_rc: int | None = None
    outcome: str | None = None
    close_row: str | None = None
    final_message: str | None = None
    final: bool = False
    error: str | None = None
    states: list[str] = Field(default_factory=list)


class ModelRemoteLaneStartResult(BaseModel):
    """The leader's answer: the outcome, its exit code, the printed lines and the final receipt."""

    model_config = _FROZEN

    outcome: EnumRemoteLaneStartOutcome
    exit_code: int
    host: str | None = None
    lines: tuple[str, ...] = ()
    receipt: ModelRemoteLaneStartReceipt


__all__ = [
    "EXIT_CODES",
    "EnumRemoteLaneStartOutcome",
    "ModelRemoteLaneStartReceipt",
    "ModelRemoteLaneStartRequest",
    "ModelRemoteLaneStartResult",
    "ModelRemoteLaneStartSettings",
]
