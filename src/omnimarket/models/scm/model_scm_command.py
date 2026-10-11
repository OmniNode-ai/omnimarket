# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the ``onex scm`` lane entry point (OMN-20913).

``onex scm <verb>`` is the one command a lane uses for GitHub work. Each verb
maps to one operation of a source-control node contract
(node_github_repo_gateway_effect for reads, node_github_repo_write_effect for
the PR authoring writes). PR and CI state reads are answered from the PR
watcher's state file and the canonical clones before any bus call; everything
else publishes the node's typed command and waits for its terminal event.

Verbs with no node operation (merge, arm, rerun, update-branch) are refused
with the place they are handled today: the landing controller.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class EnumScmNode(StrEnum):
    """The node whose contract a verb's operation belongs to."""

    GATEWAY = "node_github_repo_gateway_effect"
    WRITE = "node_github_repo_write_effect"


class EnumScmLocalRead(StrEnum):
    """The reads answered from the PR watcher state and the canonical clones."""

    PR_VIEW = "pr_view"
    PR_STATUS = "pr_status"
    PR_CHECKS = "pr_checks"
    PR_LIST = "pr_list"
    PR_MERGE_COMMIT = "pr_merge_commit"


class ModelScmVerb(BaseModel):
    """One ``onex scm`` verb and the node operation that serves it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verb: str = Field(pattern=r"^[a-z][a-z-]*$")
    node: EnumScmNode
    operation: str = Field(min_length=1)
    replaces: str = Field(min_length=1, description="The gh verb this replaces.")
    local_read: EnumScmLocalRead | None = Field(
        default=None,
        description="Answered from the watcher state and clones before the bus.",
    )


class ModelScmRefusedVerb(BaseModel):
    """A verb with no node operation, and where it is handled today."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verb: str = Field(pattern=r"^[a-z][a-z-]*$")
    handled_by: str = Field(min_length=1)


class ModelScmLocalReadRequest(BaseModel):
    """One local read: a PR (``pr_number`` set) or a repository's open PRs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read: EnumScmLocalRead
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    pr_number: int | None = Field(default=None, gt=0)
    watcher_state_path: str | None = Field(
        default=None, description="The PR watcher's state file, when one is known."
    )
    clone_path: str | None = Field(
        default=None, description="The repository's canonical clone, when present."
    )


class ModelScmLocalReadResult(BaseModel):
    """A local answer, or why there is none (the caller then asks the bus)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read: EnumScmLocalRead
    repo: str
    pr_number: int | None = None
    answered: bool
    source: Literal["watcher", "clone"] | None = None
    watcher_tick: str | None = Field(
        default=None, description="The watcher state's last_tick when it answered."
    )
    result: dict[str, JsonValue] = Field(default_factory=dict)
    unanswered_reason: str | None = None


class ModelScmBusReceipt(BaseModel):
    """Where a bus-answered verb's command and terminal event went."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    command_topic: str
    command_envelope_id: UUID
    terminal_topic: str
    terminal_envelope_id: UUID | None = None
    terminal_partition: int | None = None
    terminal_offset: str | None = None


class ModelScmTopics(BaseModel):
    """One node's command topic and its terminal topics, from its contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    success: str
    failure: str | None = None


class ModelScmTerminal(BaseModel):
    """The terminal event a bus call waited for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt: ModelScmBusReceipt
    failed: bool
    payload: dict[str, JsonValue]


class ModelScmAnswer(BaseModel):
    """What ``onex scm <verb>`` prints: one answer from one source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verb: str
    node: EnumScmNode
    operation: str
    repo: str
    source: Literal["watcher", "clone", "bus"]
    outcome: Literal["completed", "failed"]
    watcher_tick: str | None = None
    bus: ModelScmBusReceipt | None = None
    result: dict[str, JsonValue] = Field(default_factory=dict)


__all__: list[str] = [
    "EnumScmLocalRead",
    "EnumScmNode",
    "ModelScmAnswer",
    "ModelScmBusReceipt",
    "ModelScmLocalReadRequest",
    "ModelScmLocalReadResult",
    "ModelScmRefusedVerb",
    "ModelScmTerminal",
    "ModelScmTopics",
    "ModelScmVerb",
]
