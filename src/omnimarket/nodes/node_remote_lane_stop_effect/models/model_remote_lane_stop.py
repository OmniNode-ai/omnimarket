# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the remote-lane stop effect node.

No host path, operator or repository owner lives in the node: the receipt directory, the
ledger path and command, the hosts' ssh transport and the placement directory arrive on the
request, read by the caller from its overlay. The host, the pid, the run and the cloud
session are read from the lane's own receipt, never from the request.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$"

EnumStopOutcome = Literal["stopped", "already-ended", "unknown-lane", "refused"]


class ModelStopHost(BaseModel):
    """How the caller reaches one host by the name the lane's receipt gives it.

    ``ssh_argv`` is the argv prefix of the runner's ssh transport; the node sends its script
    on stdin to ``bash -s`` after it. ``work_root`` is the host's lab work root (the lane's
    context directory is ``<work_root>/runs/<run id>.ctx``). ``reap_script`` is the guard's
    reap script with a ``{unit}`` placeholder, run after the forceful signal. ``local`` marks
    the dispatching host itself, whose lanes the receipt's pid names.
    """

    model_config = _FROZEN

    name: str = Field(pattern=NAME_PATTERN)
    ssh_argv: list[str] = Field(default_factory=list)
    work_root: str = ""
    reap_script: str = ""
    local: bool = False


class ModelRemoteLaneStopRequest(BaseModel):
    """Stop the lane ``lane``, saying why and which lane asked."""

    model_config = _FROZEN

    lane: str = Field(pattern=NAME_PATTERN)
    reason: str = Field(min_length=1, max_length=300)
    stopping_lane: str = Field(pattern=NAME_PATTERN)
    receipt_dir: str
    ledger_path: str
    ledger_command: list[str] = Field(min_length=1)
    actor: str = Field(min_length=1)
    model: str = Field(min_length=1)
    hosts: list[ModelStopHost] = Field(default_factory=list)
    placement_dir: str | None = None
    cloud_command: list[str] = Field(default_factory=list)
    polite_wait_s: float = Field(default=10.0, gt=0)
    kill_wait_s: float = Field(default=10.0, gt=0)
    poll_interval_s: float = Field(default=0.5, gt=0)


class ModelRemoteLaneStopResult(BaseModel):
    """The typed outcome of one stop, with a one-line message."""

    model_config = _FROZEN

    outcome: EnumStopOutcome
    message: str
    lane: str
    run_id: str | None = None
    host: str | None = None
    terminal_ts: str | None = None
    survivor: str | None = None
    signals: list[str] = Field(default_factory=list)
