# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR watcher record's ``ci`` block, as the landing red rules read it.

Rows of ``runs`` are ``[name, status, conclusion, completed_at]``; several rows per name when older copies exist.
A key the watcher never wrote stays unset, and ``cancelled`` set to null is not the same as absent: the rules
read ``model_fields_set`` through ``model_dump(exclude_unset=True)``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelWatcherCi(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    sha: str | None = None
    red: tuple[str, ...] | None = None
    pending: tuple[str, ...] | None = None
    cancelled: tuple[str, ...] | None = None
    runs: tuple[tuple[str | None, ...], ...] | None = None


__all__: list[str] = ["ModelWatcherCi"]
