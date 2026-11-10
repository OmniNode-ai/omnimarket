# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane start request, its overlay settings, its receipt and its result (OMN-22848)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

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
    # The directory a registry root holds (the runner's marker), so a candidate that is not one is skipped.
    registry_marker: str = Field(min_length=1)
    # The repository directory that holds the host's lane-closeout command, beside the registry root; "" leaves the
    # closeout step to the frozen runner script.
    closeout_tool_repo: str = ""
    # The variable the closeout command reads the lane's ledger copy path from; all three closeout keys set, or the
    # step stays with the runner script.
    closeout_ledger_env: str = ""
    # The variable the closeout command reads the registry root from; all three closeout keys set, or the step stays
    # with the runner script.
    closeout_registry_env: str = ""
    # The plugin store on the host (path characters, optionally under $HOME): a tree is installed there from its archive;
    # "" leaves the step to the runner script.
    plugin_store: str = ""
    ssh_argv: tuple[str, ...]
    detach_argv: tuple[str, ...] = Field(min_length=1)
    # Put before the host's PATH in every host script (a non-interactive ssh shell has no user bin
    # directory); "" adds nothing. The runner script exports the same prefix before each host step.
    host_path_prefix: str
    # The host's Claude token pool, drawn through node_claude_token_pool_effect (OMN-23425): the runner exports the
    # first unlimited pool token as the engine's login; a lane that skipped the pool ran on the host login, which
    # may be at its limit while a pool label is not.
    login_pool_module: str = Field(min_length=1)
    login_pool_python_relative: str = Field(min_length=1)
    login_draw_timeout_seconds: float = Field(gt=0)
    ledger_append_argv: tuple[str, ...] = Field(min_length=1)
    # OMN-22848: "bus" on a host whose ledger of record is the bus, where a direct
    # file append from anyone but the ledger serve is refused; "local" otherwise.
    ledger_write_via: Literal["local", "bus"]
    ledger_writer_principal: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    displaces: str = Field(min_length=1)
    est_hours: int = Field(ge=1)
    priority: str = Field(min_length=1)
    claim_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def _templates_are_fillable(self) -> ModelRemoteLaneStartSettings:
        """Refuse at load a template the core cannot fill, so none is found after the CLAIM.

        The engine command and the CLAIM text are filled with ``str.format`` once the lane is
        placed; a literal brace or a field the request does not carry would raise there, after
        the CLAIM is written, and leave it open with no TERMINAL (seen on h051 2026-11-10).
        """
        for key, templates, fields in (
            ("engine_argv", self.engine_argv, {"model": "", "brief": "", "lane": ""}),
            ("claim_text", (self.claim_text,), {"host": "", "lane": ""}),
        ):
            for template in templates:
                try:
                    template.format(**fields)
                except (KeyError, IndexError, ValueError) as exc:
                    raise ValueError(
                        f"remote_lane_start.{key}: {template!r} cannot be filled from "
                        f"{sorted(fields)} ({type(exc).__name__}: {exc}); "
                        "write a literal brace doubled"
                    ) from exc
        return self

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


class ModelRemoteLaneStartFloorFill(BaseModel):
    """The ``floor_fill`` section: what the stopped-line fill runs to start a lane."""

    model_config = _FROZEN

    runner_command: tuple[str, ...] = Field(min_length=1)


class ModelRemoteLaneStartOverlay(BaseModel):
    """The whole overlay document (``overlays/node_remote_lane_start_orchestrator/overlay.yaml``).

    The overlays repository loads the document through this model, the way the node reads it: both sections
    are required, and the settings keep their per-key refusal (``from_overlay`` names a missing key).
    """

    model_config = _FROZEN

    remote_lane_start: ModelRemoteLaneStartSettings
    floor_fill: ModelRemoteLaneStartFloorFill


class ModelRemoteLaneStartRequest(BaseModel):
    """One lane to start: its name, the fill's key, its ticket, engine, placement and ledger."""

    model_config = _FROZEN

    lane: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    key: str | None = Field(default=None, min_length=1, pattern=r"^[^|\s]+$")
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
    #: The lane's runtime limit in minutes; the CLAIM's est hours follow it as the runner script's do
    #: (``max(1, round(minutes / 60))``). Absent, the overlay's ``est_hours`` stands.
    timeout_min: int | None = Field(default=None, ge=1)
    #: The host this lane repairs (OMN-23389). The lane runs on another host that reached the target over ssh,
    #: and a pin to the target names the target, not the run host.
    repair_target: str | None = Field(default=None, min_length=1)
    #: Per host, the hosts it reached over ssh, read by the caller beside the pool snapshot. A host with no entry
    #: was not tried and takes no repair lane.
    host_ssh_reach: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class ModelRemoteLaneStartReceipt(BaseModel):
    """The lane's receipt file, rewritten at each state change."""

    model_config = ConfigDict(extra="forbid")

    lane: str
    run_id: str
    key: str | None = None
    ticket: str
    state: str
    status: str | None = None
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
    "ModelRemoteLaneStartFloorFill",
    "ModelRemoteLaneStartOverlay",
    "ModelRemoteLaneStartReceipt",
    "ModelRemoteLaneStartRequest",
    "ModelRemoteLaneStartResult",
    "ModelRemoteLaneStartSettings",
]
