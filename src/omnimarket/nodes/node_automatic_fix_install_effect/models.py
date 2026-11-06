# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host-owned installation routes, immutable proof heads and tick receipts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelFixInstallTarget(BaseModel):
    """One lab install. Commands are argv, supplied by deployment, never by the bus.

    The read command prints exactly one full commit id. The installer and smoke
    commands accept ``{revision}``, replaced with the locally composed commit.
    Commands may use the existing remote installer route for a different host.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    host: str = Field(min_length=1)
    environment: Literal["lab"] = "lab"
    surface: Literal["plugin", "dispatch", "deploy-agent"]
    repository: str = Field(min_length=1)
    clone: Path
    released_ref: str = Field(min_length=1)
    installed_command: tuple[str, ...] = Field(min_length=1)
    install_command: tuple[str, ...] = Field(min_length=1)
    verify_command: tuple[str, ...] = Field(min_length=1)


class ModelFixInstallOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    state_dir: Path
    watcher_state: Path
    # Existing landing lab prover state: passes["repository#number@full-sha"].at.
    proof_state: Path
    targets: tuple[ModelFixInstallTarget, ...] = Field(min_length=1)
    max_snapshot_age_seconds: int = Field(default=480, ge=1, le=480)
    command_timeout_seconds: int = Field(default=600, ge=1, le=600)


class ModelFixInstallRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    operation: Literal["tick"] = "tick"
    apply: bool = True


class ModelFixHead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    fix: str
    head: str = Field(pattern=r"^[0-9a-f]{40}$")


class ModelFixInstallDrop(ModelFixHead):
    reason: Literal["conflict"] = "conflict"
    paths: tuple[str, ...] = ()


class ModelFixInstallReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    key: str
    host: str
    surface: str
    repository: str
    status: Literal["current", "planned", "installed", "failed", "refused"]
    previous: str = ""
    desired: str = ""
    installed: str = ""
    included: tuple[ModelFixHead, ...] = ()
    dropped: tuple[ModelFixInstallDrop, ...] = ()
    observed: str = ""
    reason: str = ""


class ModelFixInstallResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    request_id: str
    at: datetime
    status: Literal["completed", "partial", "locked", "refused"]
    targets: tuple[ModelFixInstallReceipt, ...] = ()
    findings: tuple[str, ...] = ()


class ModelFixFacts(BaseModel):
    """Only the watcher fields the installer consumes; all other fields stay ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    repo: str = ""
    state: str = ""
    head_sha: str = ""
    title: str = ""
    draft: bool | None = None
    labels: tuple[str, ...] | None = None


class ModelWatchedFix(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    facts: ModelFixFacts | None = None


class ModelFixWatcherState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    last_tick: datetime
    prs: dict[str, ModelWatchedFix]


class ModelFixProof(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    at: datetime


class ModelFixProofState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    passes: dict[str, ModelFixProof]
    fails: dict[str, object] = Field(default_factory=dict)
