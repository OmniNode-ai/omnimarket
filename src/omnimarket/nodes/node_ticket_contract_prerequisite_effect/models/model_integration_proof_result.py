# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether a ticket contract carries the integration proof its change owes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelIntegrationProofResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    runtime_paths: tuple[str, ...] = ()
    proof_item_ids: tuple[str, ...] = ()
    failure: str | None = None

    @property
    def runtime_ticket(self) -> bool:
        return bool(self.runtime_paths)

    @property
    def passed(self) -> bool:
        return self.failure is None
