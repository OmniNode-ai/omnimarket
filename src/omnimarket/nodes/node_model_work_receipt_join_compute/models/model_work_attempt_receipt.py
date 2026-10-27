# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Phase 1 receipt types; missing measurements are UNKNOWN, never zero.

Turns count assistant messages. Tool calls count Claude tool-use blocks or Codex
command/tool items. Retries count identical calls repeated after a failed result.
The runner captures those counters; legacy Codex num_turns counts stream items
and is deliberately not imported as turns. Phase 1 checks final output once.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Unknown = Literal["UNKNOWN"]
Count = Annotated[int, Field(strict=True, ge=0, le=10**15)]
Amount = Annotated[Decimal, Field(ge=0, le=Decimal("1e15"), allow_inf_nan=False)]
Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ModelFrozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelAttemptManifestRow(ModelFrozen):
    """Created before dispatch, not inferred from receipt paths or timestamps."""

    contract_id: Text
    arm: Text
    draw: Annotated[int, Field(strict=True, ge=1, le=3)]
    attempt_id: Text
    correlation_ids: tuple[UUID, ...] = ()
    landing_lease_ids: tuple[Text, ...] = ()


class ModelWorkAttemptReceipt(ModelFrozen):
    contract_id: Text
    attempt_id: Text
    arm: Text
    draw: Annotated[int, Field(strict=True, ge=1, le=3)]
    engine: Text = "UNKNOWN"
    model: Text = "UNKNOWN"
    effort: Text = "UNKNOWN"
    tokens_in: Count | Unknown = "UNKNOWN"
    tokens_out: Count | Unknown = "UNKNOWN"
    cache_read: Count | Unknown = "UNKNOWN"
    cache_write: Count | Unknown = "UNKNOWN"
    wall_s: Amount | Unknown = "UNKNOWN"
    turns: Count | Unknown = "UNKNOWN"
    tool_calls: Count | Unknown = "UNKNOWN"
    retries: Count | Unknown = "UNKNOWN"
    rejection_reasons: tuple[Text, ...] | Unknown = "UNKNOWN"
    cost_usd: Amount | Unknown = "UNKNOWN"
    cost_basis: Literal["MEASURED", "UNKNOWN"] = "UNKNOWN"
    verdict: Literal["pass", "fail", "unjoined"]
    source_refs: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def measured_cost(self) -> ModelWorkAttemptReceipt:
        if (self.cost_basis == "UNKNOWN") != (self.cost_usd == "UNKNOWN"):
            raise ValueError("A cost and its measurement basis must agree")
        return self


class ModelWorkUnjoinedReceipt(ModelFrozen):
    """A separate record type, never a per-pass denominator member."""

    record_id: Digest
    source_refs: tuple[Text, ...]
    reason: Text
    contract_id: Text = "UNKNOWN"
    attempt_id: Text = "UNKNOWN"
    verdict: Literal["unjoined"] = "unjoined"


class ModelSourceReceipt(ModelFrozen):
    family: Literal["remote_lane", "delegate", "harness_delegate", "landing"]
    source_ref: Text
    document: dict[str, JsonValue]


class ModelContractCheck(ModelFrozen):
    """Adapter from the work-contract task's pinned acceptance specification."""

    contract_id: Text
    check_sha256: Digest
    expected_exit: Annotated[int, Field(strict=True, ge=0, le=255)]


class ModelCheckExecution(ModelFrozen):
    """Lab-run check evidence, supplied separately from the model receipt.

    The comparison runner runs the contract check on final output in its fresh
    worktree. This pure node compares that execution with the pinned expectation;
    it never executes receipt-supplied commands or trusts rubric/model claims.
    """

    contract_id: Text
    attempt_id: Text
    check_sha256: Digest
    output_sha256: Digest
    exit_code: Annotated[int, Field(strict=True, ge=0, le=255)]


class ModelReceiptConflict(ModelFrozen):
    contract_id: Text
    attempt_id: Text
    field: Text
    values: tuple[str, ...]
    source_refs: tuple[Text, ...]


class ModelLandingCrossCheck(ModelFrozen):
    contract_id: Text
    attempt_id: Text
    source_ref: Text
    wall_s: Amount | Unknown


class ModelJoinSummary(ModelFrozen):
    joined: Count
    unjoined: Count
    passed: Count
    failed: Count
    per_pass_eligible: Count
    conflicts: Count


class ModelReceiptJoinPolicy(ModelFrozen):
    identity_authority: Literal["pre_dispatch_manifest"] = "pre_dispatch_manifest"
    unknown_marker: Literal["UNKNOWN"] = "UNKNOWN"
    conflict_policy: Literal["report_and_mark_unknown"] = "report_and_mark_unknown"
    landing_policy: Literal["wall_cross_check_only"] = "wall_cross_check_only"
    legacy_codex_num_turns: Literal["excluded"] = "excluded"
    writer: Literal["caller_replace_by_contract_and_attempt"] = (
        "caller_replace_by_contract_and_attempt"
    )


class ModelReceiptJoinRequest(ModelFrozen):
    policy: ModelReceiptJoinPolicy = Field(default_factory=ModelReceiptJoinPolicy)
    manifest: tuple[ModelAttemptManifestRow, ...]
    sources: tuple[ModelSourceReceipt, ...]
    contracts: tuple[ModelContractCheck, ...] = ()
    checks: tuple[ModelCheckExecution, ...] = ()
    outputs: dict[str, str] = Field(default_factory=dict)
    existing_receipts: tuple[ModelWorkAttemptReceipt, ...] = ()
    existing_unjoined: tuple[ModelWorkUnjoinedReceipt, ...] = ()

    @model_validator(mode="after")
    def unique_identity(self) -> ModelReceiptJoinRequest:
        for values in (
            [r.attempt_id for r in self.manifest],
            [c for r in self.manifest for c in r.correlation_ids],
            [c for r in self.manifest for c in r.landing_lease_ids],
            [r.contract_id for r in self.contracts],
            [(r.contract_id, r.attempt_id) for r in self.checks],
            [(r.contract_id, r.attempt_id) for r in self.existing_receipts],
        ):
            if len(values) != len(set(values)):
                raise ValueError("Ambiguous manifest or check identity")
        manifest = {(r.contract_id, r.attempt_id): r for r in self.manifest}
        for receipt in self.existing_receipts:
            row = manifest.get((receipt.contract_id, receipt.attempt_id))
            if row is None or (receipt.arm, receipt.draw) != (row.arm, row.draw):
                raise ValueError(
                    "Existing receipt identity must match the pre-dispatch manifest"
                )
        return self


class ModelReceiptJoinResult(ModelFrozen):
    receipts: tuple[ModelWorkAttemptReceipt, ...]
    unjoined: tuple[ModelWorkUnjoinedReceipt, ...]
    conflicts: tuple[ModelReceiptConflict, ...]
    landing_cross_checks: tuple[ModelLandingCrossCheck, ...]
    summary: ModelJoinSummary
