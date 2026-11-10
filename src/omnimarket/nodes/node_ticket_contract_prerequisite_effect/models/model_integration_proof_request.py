# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ticket contract and the paths its pull request changes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelIntegrationProofRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ticket_id: str
    contract_text: str
    changed_paths: tuple[str, ...] = ()
