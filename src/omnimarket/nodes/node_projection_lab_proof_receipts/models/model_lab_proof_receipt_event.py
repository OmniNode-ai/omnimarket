# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-proof-receipt event, exactly as omnibase_infra publishes it (OMN-19566).

Producer: ``scripts/ci/lab_pass_receipt.py`` ``build_pr_head_bus_event`` in
omnibase_infra, called by the lab pool driver
(``scripts/runtime_build/prepr_runtime_pool.py``) at the end of every proof, PASS
or FAIL, and published through ``scripts/ci/publish_lab_fact_event.py``.

``extra="forbid"``: a field the producer adds must be added here in the same
change, or the event dead-letters loudly instead of being half-read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from omnimarket.nodes.node_projection_lab_proof_receipts.models.enum_lab_proof_receipt_result import (
    EnumLabProofReceiptResult,
)

_SHA = r"^[0-9a-f]{40}$"


def receipt_key_text(
    repo: str, pr_number: int, head_sha: str, profile_id: str, profile_version: str
) -> str:
    """``<repo>#<pr>@<head>:<profile>@<version>``, the producer's key spelling."""
    return f"{repo}#{pr_number}@{head_sha}:{profile_id}@{profile_version}"


class ModelLabProofReceiptEvent(BaseModel):
    """One pr-head receipt as a bus fact; ``receipt`` is the whole minted receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0.0"]
    event_type: Literal["lab-proof-receipt"]
    topic: str = Field(min_length=1)
    lane: Literal["pr-head"]
    receipt_key: str = Field(min_length=1)
    repo: str = Field(pattern=r"^OmniNode-ai/[A-Za-z0-9._-]+$")
    pr_number: int = Field(ge=1)
    head_sha: str = Field(pattern=_SHA)
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    handler_kind: str = Field(min_length=1)
    result: EnumLabProofReceiptResult
    verifier_token: str = Field(min_length=1)
    verifier_reason: str = Field(min_length=1)
    mandatory_checks: tuple[str, ...]
    missing_mandatory_checks: tuple[str, ...]
    failing_checks: tuple[str, ...]
    started_at: datetime
    finished_at: datetime
    runner_identity: str = Field(min_length=1)
    verifier_identity: str = Field(min_length=1)
    host: str = Field(min_length=1)
    slot: str = Field(min_length=1)
    carried_from: str = ""
    receipt: dict[str, JsonValue]

    @model_validator(mode="after")
    def _self_consistent(self) -> ModelLabProofReceiptEvent:
        # The key is recomputed, never trusted: a key naming another head or
        # profile than the fields beside it would file the proof under a row it
        # does not prove.
        expected = receipt_key_text(
            self.repo,
            self.pr_number,
            self.head_sha,
            self.profile_id,
            self.profile_version,
        )
        if self.receipt_key != expected:
            raise ValueError(
                f"receipt_key {self.receipt_key!r} does not match its own fields "
                f"({expected!r})"
            )
        if self.receipt.get("sha") != self.head_sha:
            raise ValueError("the embedded receipt proves another head than head_sha")
        if self.receipt.get("result") != self.result.value:
            raise ValueError("the embedded receipt's result differs from result")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        # A receipt whose runner is its own verifier, or one missing a mandatory
        # check, is NOT refused here: it is recorded with the verifier's token
        # (RUNNER_IS_VERIFIER, MISSING_MANDATORY_CHECK, ...), because a refused
        # proof that leaves no row is indistinguishable from one never run.
        return self


__all__ = ["ModelLabProofReceiptEvent", "receipt_key_text"]
