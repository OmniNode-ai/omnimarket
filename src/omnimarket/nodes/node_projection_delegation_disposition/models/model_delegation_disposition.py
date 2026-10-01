# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a disposition request, row and fold result."""

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.events.delegation_disposition import ModelDelegationDispositionRecorded
from omnimarket.projection.envelope import strip_runner_injected_keys

# OMN-20012: the producers publish through node_event_emit_effect, whose
# publish-time enrichment (enrichment.ENRICHMENT_FIELDS, a wire contract ported
# from the emit daemon) adds these keys to every payload. The disposition event
# names none of them, so without this strip every published disposition was
# refused here as extra fields and never reached delegation_dispositions.
# Listed rather than imported because one node does not import another's
# module; tests/test_omn20012_disposition_emit_enrichment.py asserts this set
# equals ENRICHMENT_FIELDS, so the two cannot drift apart silently. The
# delegation's own identity is delegation_correlation_id, never correlation_id.
EMIT_ENRICHMENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "correlation_id",
        "causation_id",
        "emitted_at",
        "session_id",
        "entity_id",
        "schema_version",
    }
)


class ModelDelegationDispositionProjectionRequest(ModelDelegationDispositionRecorded):
    """Strip runtime and emitter metadata; producer time remains required."""

    @model_validator(mode="before")
    @classmethod
    def _accept_runtime_metadata(cls, data: Any) -> Any:
        if isinstance(data, dict):
            stripped = strip_runner_injected_keys(data)
            return {k: v for k, v in stripped.items() if k not in EMIT_ENRICHMENT_KEYS}
        return data


class ModelDelegationDispositionRow(ModelDelegationDispositionRecorded):
    """One current disposition per tenant and delegation correlation."""


class ModelDelegationDispositionProjectionResult(BaseModel):
    """Purely derived rows for the effect writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    rows: tuple[ModelDelegationDispositionRow, ...]
