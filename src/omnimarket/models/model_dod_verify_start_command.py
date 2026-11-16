"""ModelDodVerifyStartCommand — command to start DoD verification."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from omnibase_core.enums.ticket.enum_dod_check_type import EnumDodCheckType
from omnibase_core.models.ticket.model_contract_dod_item import ModelContractDodItem
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class ModelDodVerifyStartCommand(BaseModel):
    """Command to start DoD evidence verification.

    ``ticket_id`` is the only caller-supplied field required for routing.
    ``correlation_id`` and ``requested_at`` default when absent so the typed
    command validates against a bare ``onex run-node`` payload such as
    ``{"ticket_id": "OMN-1234", "contract_path": null}`` (OMN-12420). The
    runtime supplies its own envelope correlation_id; these defaults only cover
    the in-process / direct-dispatch paths where the caller omits them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket ID (e.g. OMN-1234).")
    correlation_id: UUID = Field(
        default_factory=uuid4, description="Verification run correlation ID."
    )
    contract_path: str | None = Field(
        default=None, description="Override path to contract YAML."
    )
    dry_run: bool = Field(default=False)
    execution_audience: EnumDodVerifyExecutionAudience | None = Field(
        default=None,
        description=(
            "Authorized evidence execution boundary. Hosted and local Done-gate "
            "entry points must set this explicitly; omission fails closed."
        ),
    )
    # OMN-19514: the delegation run whose output this verification judges --
    # the delegate-skill correlation id, which is the delegation_events key.
    # The verifier's own correlation_id identifies the verification run, not
    # the attempt, so without this no verdict can be joined to the delegated
    # attempt it judged. Omitted from serialisation when unset, so every
    # existing caller and every consumer predating the field sees the shape it
    # saw before.
    delegation_correlation_id: UUID | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Correlation id of the delegation run this verification judges.",
    )
    goal_id: UUID | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Goal being verified.",
    )
    parent_goal_id: UUID | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Parent goal for this verification, when nested.",
    )
    level: Literal["delegate_call", "workflow_lane", "interactive_session"] | None = (
        Field(
            default=None,
            exclude_if=lambda value: value is None,
            description="Goal level being verified.",
        )
    )
    contract_revision: UUID | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Immutable revision of the goal contract.",
    )
    contract_schema_version: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Declared schema version of the goal contract being verified.",
    )
    dod_evidence: tuple[ModelContractDodItem, ...] = Field(
        default=(), description="Inline goal contract items, instead of contract_path."
    )
    requested_at: datetime = Field(
        default_factory=_utc_now, description="When the command was issued."
    )

    @field_validator("contract_schema_version")
    @classmethod
    def _validate_contract_schema_version(cls, value: str | None) -> str | None:
        if value is not None:
            return ModelTicketContract.model_validate(
                {
                    "ticket_id": "OMN-1",
                    "title": "goal contract schema version",
                    "schema_version": value,
                }
            ).schema_version
        return None

    @model_validator(mode="after")
    def _validate_contract_source(self) -> ModelDodVerifyStartCommand:
        has_goal = self.goal_id is not None
        if has_goal:
            if self.contract_path is not None:
                raise ValueError("goal-scoped verification cannot set contract_path")
            if not self.dod_evidence:
                raise ValueError("goal-scoped verification requires dod_evidence")
            if self.contract_revision is None:
                raise ValueError("goal-scoped verification requires contract_revision")
            if self.contract_schema_version is None:
                raise ValueError(
                    "goal-scoped verification requires contract_schema_version"
                )
            if self.level is None:
                raise ValueError("goal-scoped verification requires level")
            for item in self.dod_evidence:
                check_types = {check.check_type for check in item.checks}
                if EnumDodCheckType.DISPOSITION in check_types and check_types != {
                    EnumDodCheckType.DISPOSITION
                }:
                    raise ValueError(
                        "disposition items cannot mix disposition and executable checks"
                    )
            return self

        if (
            self.dod_evidence
            or self.parent_goal_id
            or self.contract_revision
            or self.contract_schema_version
            or self.level
        ):
            raise ValueError("goal fields require goal_id")
        return self


__all__: list[str] = ["ModelDodVerifyStartCommand"]
