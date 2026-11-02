# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared-clone refresh request and receipt (OMN-22149).

Coverage comes from the lab host table, not from a directory scan: each host row's ``clones`` column
names the clone of a declared repository role and the path it sits at. One request refreshes the
declared clone of each named host (every host that declares one when none is named); the receipt
lists every host with its before and after sha, and a host whose clone cannot be brought to the
origin tip is a typed refusal that names the host and the path, never a clean result.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumDeclaredCloneState(StrEnum):
    """What one run did to one host's declared clone."""

    UP_TO_DATE = "up_to_date"
    REFRESHED = "refreshed"
    REFUSED = "refused"


class EnumDeclaredCloneRefusal(StrEnum):
    """Why a declared clone was not brought to the origin tip."""

    NOT_DECLARED = "not_declared"
    HOST_UNREACHABLE = "host_unreachable"
    PATH_UNRESOLVED = "path_unresolved"
    PATH_MISSING = "path_missing"
    NOT_A_CLONE = "not_a_clone"
    FETCH_FAILED = "fetch_failed"
    NOT_FAST_FORWARDED = "not_fast_forwarded"
    UNREADABLE_RESULT = "unreadable_result"


class ModelDeclaredCloneRefreshRequest(BaseModel):
    """Refresh the declared clone of ``hosts``; no host named means every host that declares one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[str, ...] = Field(default=(), max_length=64)


class ModelDeclaredCloneOutcome(BaseModel):
    """One host's declared clone after the run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    path: str
    state: EnumDeclaredCloneState
    refusal: EnumDeclaredCloneRefusal | None = None
    before_sha: str = ""
    after_sha: str = ""
    origin_sha: str = ""
    message: str = Field(default="", max_length=2000)


class ModelDeclaredCloneRefreshReceipt(BaseModel):
    """What one run did across the hosts, published on the declared-clone receipt topic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcomes: tuple[ModelDeclaredCloneOutcome, ...]
    duration_ms: int = Field(ge=0)

    # True when any host is a refusal, or none was run: the run is never a clean result then. Derived
    # from the outcomes when the receipt is built, so a copy read off the wire agrees with them.
    refused: bool = False

    @model_validator(mode="before")
    @classmethod
    def _derive_refused(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        outcomes = data.get("outcomes") or ()
        refused = not outcomes or any(
            (o.get("state") if isinstance(o, dict) else o.state)
            == EnumDeclaredCloneState.REFUSED
            for o in outcomes
        )
        return {**data, "refused": refused}


class ModelDeclaredCloneTopics(BaseModel):
    """The declared-clone refresh topics, read from the node contract and nowhere else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: str
    receipt: str
    repo_role: str


__all__ = [
    "EnumDeclaredCloneRefusal",
    "EnumDeclaredCloneState",
    "ModelDeclaredCloneOutcome",
    "ModelDeclaredCloneRefreshReceipt",
    "ModelDeclaredCloneRefreshRequest",
    "ModelDeclaredCloneTopics",
]
