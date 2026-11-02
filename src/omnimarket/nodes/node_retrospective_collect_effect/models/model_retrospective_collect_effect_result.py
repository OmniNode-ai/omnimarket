# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result of node_retrospective_collect_effect: the key fields of the crossing it publishes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelRetrospectiveCollectEffectResult(BaseModel):
    """Key fields of the published crossing; a field the node does not hold yet is unset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    correlation_id: str
    input_hash: str | None = None


__all__ = ["ModelRetrospectiveCollectEffectResult"]
