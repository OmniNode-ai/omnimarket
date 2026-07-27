# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the delegation inference response projection reducer (OMN-13088)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

REDUCER_VERSION = "1.0.0"

#: Number of recent inference responses retained in the snapshot window.
MAX_HISTORY: int = 10

#: OMN-13088 legacy singleton key, retained only as the pre-tranche-2 default
#: for ModelInferenceResponseProjectionResult.singleton_key documentation.
#: The table itself is no longer a single global singleton as of OMN-14894
#: tranche 2 -- see DEFAULT_TENANT and the handler's per-tenant re-key.
SINGLETON_KEY: str = "global"

#: OMN-14894 (tranche 2): interim single-tenant fallback used as both the
#: tenant_id value and the singleton_key/conflict-key value when an
#: inference-response event carries no tenant_id. Mirrors the DEFAULT
#: 'omninode' convention already used by delegation_events (0022) and
#: delegation_budget_state (0019).
DEFAULT_TENANT: str = "omninode"


class ModelRecentInferenceResponse(BaseModel):
    """One entry in the rolling recent-responses window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    model_name: str
    task_type: str = Field(default="")
    generated_text: str
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    captured_at: datetime


class ModelInferenceResponseProjectionResult(BaseModel):
    """Return value from a single handler invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    singleton_key: str = Field(default=SINGLETON_KEY)
    skipped: bool = Field(default=False)


__all__: list[str] = [
    "DEFAULT_TENANT",
    "MAX_HISTORY",
    "REDUCER_VERSION",
    "SINGLETON_KEY",
    "ModelInferenceResponseProjectionResult",
    "ModelRecentInferenceResponse",
]
