# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``claim_index`` overlay: where the rolling work ledger is read from.

Shape only; the deployment owner supplies the values. There is no packaged default for either path.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelClaimIndexConfig(BaseModel):
    """``ledger_path`` is the live ledger file; ``index_path`` is the optional cache of the derived index."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_path: str = Field(
        min_length=1,
        description="The live ledger file; rolled archives sit in an archive/ directory beside it.",
    )
    index_path: str | None = Field(
        default=None,
        min_length=1,
        description="Where the derived index is cached; absent means the index is rebuilt on every question.",
    )
