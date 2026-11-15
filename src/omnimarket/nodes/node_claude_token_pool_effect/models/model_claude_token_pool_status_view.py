# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the pool status view is asked, and what it answers (OMN-19998).

A host that asks after the pool may hold a local copy whose limited and revoked marks are stale: the
view names which answer its result is built on (``basis``) and, when the local copy is all it has,
refuses to name any limited or revoked label at all. No model here holds a token: labels, counts and
states only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelPoolStatusViewRequest(BaseModel):
    """Which host's pool status the view is asked for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)


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
    "ModelPoolStatusViewRequest",
    "ModelPoolStatusViewResult",
]
