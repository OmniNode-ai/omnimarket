# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host-owned installation routes, immutable proof heads and tick receipts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
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


class EnumJoinJ9Outcome(StrEnum):
    """What the J9 join reads for one merged head across the declared install targets."""

    INSTALLED = "installed"
    FAILED = "failed"
    PENDING = "pending"


class ModelJoinJ9Policy(BaseModel):
    """The contract's ``join_j9`` section: the two piece numbers, the hour and the typed failure codes."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    join_id: str = Field(pattern=r"^J\d{1,2}$")
    from_piece: int = Field(ge=1, le=18)
    to_piece: int = Field(ge=1, le=18)
    emission_node: str = Field(min_length=1)
    deadline_seconds: int = Field(ge=1)
    late_failure_code: str = Field(min_length=1)
    mismatch_failure_code: str = Field(min_length=1)
    refused_failure_code: str = Field(min_length=1)


class ModelJoinJ9Target(BaseModel):
    """One declared install target the merged head must reach: the overlay target's key and its host."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    key: str = Field(min_length=1)
    host: str = Field(min_length=1)


class ModelJoinJ9Reading(BaseModel):
    """One receipt the installer wrote, with the time it wrote it."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    at: datetime
    receipt: ModelFixInstallReceipt


class ModelJoinJ9Request(BaseModel):
    """A merged head, when it merged, the targets it must reach and the receipts read so far."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: str = Field(min_length=1)
    merged_head: str = Field(pattern=r"^[0-9a-f]{40}$")
    merged_at: datetime
    judged_at: datetime
    targets: tuple[ModelJoinJ9Target, ...] = Field(min_length=1)
    readings: tuple[ModelJoinJ9Reading, ...] = ()


class ModelJoinJ9Failure(BaseModel):
    """A target that did not install the merged head inside the deadline, named with a typed code."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    key: str
    host: str
    code: str
    detail: str


class ModelJoinJ9Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    outcome: EnumJoinJ9Outcome
    correlation_id: str
    merged_head: str
    deadline_at: datetime
    installed: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    failures: tuple[ModelJoinJ9Failure, ...] = ()
    failure_code: str | None = None
