from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.events.dod_verify_retry import (
    EnumDodVerifyRetryDisposition,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.services.gate_escape_audit import (
    ModelGateEscapeFinding,
)


class ModelDodCheckResult(BaseModel):
    """Result for a single DoD check against one ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: str = Field(
        description="Check identifier (e.g. contract_exists, pr_merged)."
    )
    status: str = Field(description="pass | fail | skip")
    details: dict[str, str] = Field(
        default_factory=dict,
        description="Machine-readable check details.",
    )


class ModelDodTicketResult(BaseModel):
    """Aggregated DoD check results for a single ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(description="Ticket ID.")
    status: str = Field(
        description="verified | failed | skipped | unresolved (OMN-17022)"
    )
    checks: tuple[ModelDodCheckResult, ...] = Field(default=())
    receipt_path: str = Field(
        default="", description="Written or planned receipt path."
    )
    receipt_written: bool = Field(default=False)
    failed: int = Field(default=0)
    skipped: int = Field(default=0)
    # OMN-17022 (off-rails A15). Before these fields, a ticket whose sweep run
    # faulted was reported with whatever partial checks had accumulated, and
    # nothing on the result said the run had not finished — which is how ten
    # items were "held unadjudicated" with no machine-readable trace of why.
    unresolved_cause: EnumDodVerifyUnresolvedCause | None = Field(
        default=None,
        description="Why the run reached no verdict. Set iff status is 'unresolved'.",
    )
    retry_disposition: EnumDodVerifyRetryDisposition | None = Field(
        default=None,
        description="What reconciliation decided for this item this pass.",
    )
    attempt_count: int = Field(
        default=0,
        ge=0,
        description="Attempts recorded for this item, including this one.",
    )
    next_attempt_not_before: str = Field(
        default="",
        description=(
            "ISO-8601 instant the next attempt may run. Non-empty only when "
            "retry_disposition is RETRY_SCHEDULED."
        ),
    )
    retry_reason: str = Field(
        default="", description="Human-readable justification for the disposition."
    )

    @model_validator(mode="after")
    def _cause_pairs_with_unresolved(self) -> Self:
        """An untyped 'unresolved' is the ad-hoc label this ticket removes."""
        unresolved = self.status == "unresolved"
        if unresolved != (self.unresolved_cause is not None):
            raise ValueError(
                "unresolved_cause is set exactly when status is 'unresolved'; "
                f"got status={self.status!r}, "
                f"unresolved_cause={self.unresolved_cause!r} for {self.ticket_id}"
            )
        return self


class ModelDodSweepOrchestratorResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="", description="Sweep result status.")
    mode: str = Field(default="targeted", description="targeted | batch")
    ticket_id: str = Field(
        default="", description="Target ticket ID for targeted sweeps."
    )
    receipt_path: str = Field(
        default="", description="Written or planned receipt path."
    )
    receipt_written: bool = Field(
        default=False, description="Whether a receipt was written."
    )
    contract_path: str = Field(default="", description="Resolved ticket contract path.")
    contract_exists: bool = Field(
        default=False, description="Whether the ticket contract exists."
    )
    failed: int = Field(default=0, description="Number of failed deterministic checks.")
    skipped: int = Field(
        default=0, description="Number of skipped deterministic checks."
    )
    details: dict[str, str] = Field(
        default_factory=dict,
        description="Machine-readable sweep details.",
    )
    # Batch mode fields
    batch_results: tuple[ModelDodTicketResult, ...] = Field(
        default=(),
        description="Per-ticket results in batch mode.",
    )
    batch_total: int = Field(default=0, description="Total tickets processed in batch.")
    batch_failed: int = Field(
        default=0, description="Tickets with at least one failed check."
    )
    batch_verified: int = Field(
        default=0, description="Tickets with all checks passing."
    )
    # OMN-17022: an unresolved item blocks any "sweep clean" claim. Counted on
    # its own axis and never folded into batch_failed — a run that faulted is
    # not a red about the product, and reporting it as one is what made the
    # ten held items look adjudicated when nothing had been adjudicated.
    batch_unresolved: int = Field(
        default=0,
        description="Tickets that reached no verdict (status 'unresolved').",
    )
    # Gate-escape audit fields (OMN-13854, mode == "gate_escape_audit")
    gate_escape_findings: tuple[ModelGateEscapeFinding, ...] = Field(
        default=(),
        description="Per-ticket gate-escape audit verdicts when mode is gate_escape_audit.",
    )
    gate_escape_checked: int = Field(
        default=0, description="Total Done tickets evaluated by the gate-escape audit."
    )
    gate_escape_flagged: int = Field(
        default=0,
        description="Tickets flagged as gate-escape candidates (wf_1628d9a5 signature).",
    )
