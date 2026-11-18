# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the pool status view is asked, and what it answers (OMN-19998).

A host that asks after the pool may hold a local copy whose limited and revoked marks are stale: the
view names which answer its result is built on (``basis``) and, when the local copy is all it has,
refuses to name any limited or revoked label at all. No model here holds a token: labels, counts and
states only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelPoolStatusViewRequest(BaseModel):
    """Which host's pool status the view is asked for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)


class ModelPoolHostLogin(BaseModel):
    """Which pool label one host's login is, as that host's own tick last published it. A label, never a token."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    #: The pool label whose fingerprint equals the host's login; ``None`` when the login is no token of the pool.
    label: str | None
    at: datetime


class ModelPoolLabelState(BaseModel):
    """One account label as the operator reads it: its limit state, the reset it reads, and the hosts on it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    state: Literal["usable", "limited", "revoked", "challenged"]
    #: When a ``limited`` label's limit ends (UTC stamp); ``None`` for every other state.
    reset: str | None
    #: The hosts whose latest fresh login record names this label.
    hosts: tuple[str, ...]


class ModelPoolStatusViewResult(BaseModel):
    """The pool as one basis shows it. Labels, counts and states only, never a token.

    ``local_pool``: the overlay declares the host, and its own pool file is authoritative.
    ``source_marks``: the host's copy was judged against the overlay's one marks source, read fresh
    and applied to a view of the pool. ``unsynced_local_copy``: the copy on disk is all there is —
    the overlay was unreadable on this host, the source could not be read, or it is over its age
    bound — so no limited or revoked label is named from it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    basis: Literal["local_pool", "source_marks", "unsynced_local_copy"]
    #: The host the marks were read from, on a ``source_marks`` answer.
    marks_source_host: str | None
    #: How old the read marks were, in seconds, on a ``source_marks`` answer.
    marks_age_seconds: int | None
    #: How old the local pool file on disk is, when its write time is known; ``None`` when it is not.
    local_copy_age_seconds: int | None
    #: On an ``unsynced_local_copy`` answer, why the marks were not read.
    detail: str = ""
    tokens: int
    labels: tuple[str, ...]
    limited: tuple[str, ...] | None
    revoked: tuple[str, ...] | None
    challenged: tuple[str, ...] | None
    status_line: str
    #: One entry per pool label, in the pool's order; empty on an ``unsynced_local_copy`` answer (no mark is named from it).
    label_states: tuple[ModelPoolLabelState, ...] = ()
    #: The one line naming every label with its state, reset and hosts; on an unsynced answer, the line saying so.
    labels_line: str = ""
    #: The overlay hosts with no login record fresh enough to place them, so no label above counts them.
    hosts_unread: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _unsynced_copy_names_no_mark(self) -> ModelPoolStatusViewResult:
        """An unsynced copy never names a limited or revoked label; every other basis must."""
        if self.basis == "unsynced_local_copy":
            if (
                self.limited is not None
                or self.revoked is not None
                or self.challenged is not None
            ):
                raise ValueError(
                    "basis unsynced_local_copy names no limited or revoked label: "
                    "limited, revoked and challenged must be None"
                )
            return self
        if self.limited is None or self.revoked is None or self.challenged is None:
            raise ValueError(
                f"basis {self.basis} must name its limited, revoked and challenged labels"
            )
        return self


__all__ = [
    "ModelPoolHostLogin",
    "ModelPoolLabelState",
    "ModelPoolStatusViewRequest",
    "ModelPoolStatusViewResult",
]
