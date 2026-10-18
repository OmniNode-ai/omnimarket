# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The facts of one session start, as the hook receives them."""

from pydantic import BaseModel, ConfigDict, Field


class ModelSessionCapabilityDigestRequest(BaseModel):
    """One session start: where it opened, how it started and where the digest lives."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cwd: str = Field(description="Working directory the session opened in.")
    source: str = Field(
        default="startup",
        description="How the session started: startup, resume, clear or compact.",
    )
    registry_roots: str = Field(
        default="",
        description="Colon-separated absolute registry roots; the digest prints only inside one.",
    )
    state_dir: str = Field(
        default="",
        description="State root holding the capability directory; empty when the environment names none.",
    )
    uid: int = Field(
        default=0,
        ge=0,
        description="User id the launchd fix line names.",
    )
    now_epoch: float | None = Field(
        default=None,
        description="Clock override in epoch seconds for tests; the handler reads the wall clock when None.",
    )
