# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable observations, contract rows and client verdicts (OMN-21098)."""

from enum import StrEnum
from string import Formatter

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.claude_code_binding import (
    EnumClientMaxMode,
    EnumClientVerdictAction,
)


class EnumDelegationClientOutcome(StrEnum):
    """What the adapter observed while waiting for admission and completion."""

    REFUSED_BEFORE_PUBLISH = "refused_before_publish"
    ADMISSION_UNKNOWN = "admission_unknown"
    NOT_ADMITTED = "not_admitted"
    ADMISSION_OBSERVE = "admission_observe"
    TERMINAL = "terminal"
    TERMINAL_TIMEOUT = "terminal_timeout"


class EnumDelegationClientVerdictRow(StrEnum):
    """Row keys in the contract's client verdict table."""

    REFUSED_BEFORE_PUBLISH = "refused_before_publish"
    ADMISSION_UNKNOWN = "admission_unknown"
    NOT_ADMITTED = "not_admitted"
    ADMISSION_OBSERVE = "admission_observe"
    TERMINAL_ACCEPTED = "terminal_accepted"
    TERMINAL_REJECTED = "terminal_rejected"
    TERMINAL_IDENTITY_MISMATCH = "terminal_identity_mismatch"
    TERMINAL_TIMEOUT = "terminal_timeout"
    EFFECTIVE_MODE_ABOVE_MAX = "effective_mode_above_max"
    INCONSISTENT_OBSERVATION = "inconsistent_observation"


# The only actions a delegation verdict may name; the others belong to other bindings.
DELEGATION_VERDICT_ACTIONS = frozenset(
    {
        EnumClientVerdictAction.PROCEED_NATIVE,
        EnumClientVerdictAction.ANSWER,
        EnumClientVerdictAction.REFUSE,
    }
)


class ModelDelegationClientVerdictRequest(BaseModel):
    """Facts observed by the client; no admission echo means no effective mode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    client_max_mode: EnumClientMaxMode
    effective_mode: EnumClientMaxMode | None = None
    outcome: EnumDelegationClientOutcome
    task_type_allowlisted: bool = False
    admission_reason: str = ""
    terminal_status: str = ""
    terminal_quality_gate_passed: bool = False
    terminal_failure_cause: str = ""
    client_call_key: str = ""
    request_fingerprint: str = ""
    echoed_client_call_key: str = ""
    echoed_request_fingerprint: str = ""


class ModelDelegationClientVerdictRow(BaseModel):
    """One contract cell; a null allowlist selector matches either value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: EnumDelegationClientVerdictRow
    mode: EnumClientMaxMode
    allowlisted: bool | None = None
    action: EnumClientVerdictAction
    reason: str = ""

    @field_validator("action")
    @classmethod
    def validate_action(cls, value: EnumClientVerdictAction) -> EnumClientVerdictAction:
        """A delegation cell answers, refuses or proceeds natively, nothing else."""
        if value not in DELEGATION_VERDICT_ACTIONS:
            raise ValueError(f"not a delegation verdict action: {value}")
        return value

    @field_validator("reason")
    @classmethod
    def validate_reason_template(cls, value: str) -> str:
        """Allow only the four request placeholders declared by the contract."""
        allowed = {
            "correlation_id",
            "admission_reason",
            "terminal_status",
            "terminal_failure_cause",
        }
        for _, field, format_spec, conversion in Formatter().parse(value):
            if field is not None and (
                field not in allowed or format_spec or conversion is not None
            ):
                raise ValueError(f"unsupported reason placeholder: {field}")
        return value


class ModelDelegationClientVerdictResult(BaseModel):
    """The client action and the contract row and mode used to decide it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: EnumClientVerdictAction
    reason: str
    row: EnumDelegationClientVerdictRow
    mode: EnumClientMaxMode
    correlation_id: str
