# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared overlay model of the ledger rows loader, read by the loader node and its settings node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRowsLoaderOverlay(BaseModel):
    """Every deployment fact of the node; no key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # True until cutover plan step L15; from L15 a non-ok node answer is unreadable, no file read.
    file_fallback_allowed: bool
    # The environment variable naming the file the node's events are journalled to.
    journal_env: str = Field(min_length=1)
