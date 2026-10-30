# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the session checkpoint node.

Operations: ``write`` a checkpoint, ``read`` the newest one the session owns (with the prose-handoff
fallback when there is none), ``validate`` one file, and ``record`` a PostToolUse payload (a
Workflow launch or a TaskStop) into the session's checkpoint. Every operation is bound to the
session's principals through the team identity declaration first.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_team_identity_compute.models import ModelIdentityClaim
from omnimarket.session_scope import ModelSessionScope

Operation = Literal["write", "read", "validate", "record"]


class ModelCheckpointWriteSpec(BaseModel):
    """``write``: the lane, and ``key=value;key=value`` items added to the starting checkpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    ticket: str | None = None
    session_id: str | None = None
    from_file: str | None = None
    workflows: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    deadlines: tuple[str, ...] = ()
    ticks: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class ModelCheckpointHookSpec(BaseModel):
    """``record``: the PostToolUse payload and the gates the hook applies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload: dict[str, Any]
    entrypoint: str | None = None  # CLAUDE_CODE_ENTRYPOINT
    hook_mode: Literal["off", "force"] | None = None  # OMNI_SESSION_START_HOOK
    omni_home: str | None = None
    landing_worker: bool = False  # LANDING_WORKER_TAG is set
    sessions_dir: str | None = None  # where */session.env live


class ModelSessionCheckpointRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Operation
    directory: str  # the checkpoint directory, $OMNI_HOME/.claude_scratch/handoffs
    identity_claims: tuple[ModelIdentityClaim, ...] = ()
    overlay_roots: tuple[str, ...] | None = None
    now: str | None = None
    path: str | None = None  # read or validate one file
    write: ModelCheckpointWriteSpec | None = None
    hook: ModelCheckpointHookSpec | None = None


class ModelSessionCheckpointResult(BaseModel):
    """``ok``; ``absent`` (no checkpoint this session owns); ``skipped``; ``refused`` with findings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Operation
    status: Literal["ok", "absent", "skipped", "refused", "error"]
    scope: ModelSessionScope
    path: str | None = None
    lines: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    prose_handoff: str | None = None
    message: str = ""
