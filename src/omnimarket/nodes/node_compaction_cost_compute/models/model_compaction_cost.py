# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and cost lines for compaction cost accounting (OMN-21966).

Every token count the caller passes is a usage record or a tool-call result size
with the provenance it was read under; the node reads no transcript, receipt or
hook event itself. A record that is absent, or present without its counts, is
``unavailable`` and never zero. Boundary ``preTokens`` is the size of the context
that was summarized and is never a summarizer usage record.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

Count = Annotated[int, Field(strict=True, ge=0)]
Nonempty = Annotated[str, Field(min_length=1)]

PERTURBATION_FRACTION = 0.5


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumProvenance(StrEnum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class EnumUsageKind(StrEnum):
    SUMMARIZER_CALL = "summarizer_call"
    PACK_BUILD = "pack_build"
    ROUTED_CALL = "routed_call"


class EnumRecoveryKind(StrEnum):
    LEDGER_READ = "ledger_read"
    CHECKPOINT_READ = "checkpoint_read"
    LEDGER_ASK = "ledger_ask"
    CATCHUP_SKILL = "catchup_skill"
    RESUME_SKILL = "resume_skill"
    HANDOFF_READ = "handoff_read"


class EnumRatioStatus(StrEnum):
    MEASURED = "MEASURED"
    ESTIMATED = "ESTIMATED"
    UNAVAILABLE = "UNAVAILABLE"


class EnumSafeguard(StrEnum):
    ESTABLISHED = "ESTABLISHED"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


class ModelUsageRecord(_Frozen):
    """One call's token usage and where it was read from."""

    request_id: Nonempty
    kind: EnumUsageKind
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    source: Nonempty


class ModelBoundary(_Frozen):
    """The compact_boundary record's metadata, with its transcript field names."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    uuid: Nonempty
    pre_tokens: Count | None = Field(default=None, alias="preTokens")
    post_tokens: Count | None = Field(default=None, alias="postTokens")


class ModelToolCall(_Frozen):
    """A tool call after the boundary and the input size of its result."""

    tool_name: Nonempty
    target: str = ""
    result_input_tokens: Count | None = None
    provenance: EnumProvenance = EnumProvenance.MEASURED

    @model_validator(mode="after")
    def _counted_provenance(self) -> ModelToolCall:
        if self.provenance is EnumProvenance.UNAVAILABLE:
            raise ValueError(
                "A tool call carries measured or estimated provenance; "
                "a missing count is result_input_tokens=None"
            )
        return self


class ModelTurn(_Frozen):
    index: Annotated[int, Field(strict=True, ge=0)]
    tool_calls: tuple[ModelToolCall, ...] = ()


class ModelRecoveryPattern(_Frozen):
    """One entry of the recovery-call list: a tool name set and an optional target regex."""

    pattern_id: Nonempty
    kind: EnumRecoveryKind
    tool_names: tuple[Nonempty, ...] = Field(min_length=1)
    target_regex: str | None = None

    @model_validator(mode="after")
    def _regex_compiles(self) -> ModelRecoveryPattern:
        if self.target_regex is not None:
            try:
                re.compile(self.target_regex)
            except re.error as error:
                raise ValueError(f"target_regex does not compile: {error}") from error
        return self


class ModelArmInput(_Frozen):
    """One arm's compaction: its boundary, usage records and the turns after it."""

    session_id: Nonempty
    boundary: ModelBoundary
    summary_text: str | None = None
    usage_records: tuple[ModelUsageRecord, ...] = ()
    turns: tuple[ModelTurn, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> ModelArmInput:
        ids = [record.request_id for record in self.usage_records]
        if len(ids) != len(set(ids)):
            raise ValueError("Usage record request ids must be unique")
        indexes = [turn.index for turn in self.turns]
        if len(indexes) != len(set(indexes)):
            raise ValueError("Turn indexes must be unique")
        return self


_STOCK_KINDS = frozenset({EnumUsageKind.SUMMARIZER_CALL})
_MOD_KINDS = frozenset({EnumUsageKind.PACK_BUILD, EnumUsageKind.ROUTED_CALL})


class ModelCompactionCostRequest(_Frozen):
    """The stock arm, the mod arm or both, and the section 3.8 bound if one applies."""

    stock: ModelArmInput | None = None
    mod: ModelArmInput | None = None
    window_turns: Annotated[int, Field(strict=True, ge=1)] = 25
    recovery_patterns: tuple[ModelRecoveryPattern, ...] | None = None
    safeguard_upper_bound: Annotated[float, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def _arms(self) -> ModelCompactionCostRequest:
        if self.stock is None and self.mod is None:
            raise ValueError("At least one arm is required")
        if self.stock is not None and any(
            r.kind not in _STOCK_KINDS for r in self.stock.usage_records
        ):
            raise ValueError("The stock arm takes summarizer_call usage records only")
        if self.mod is not None and any(
            r.kind not in _MOD_KINDS for r in self.mod.usage_records
        ):
            raise ValueError(
                "The mod arm takes pack_build and routed_call usage records only"
            )
        return self


class ModelCostLine(_Frozen):
    """A value with its provenance and the basis it was taken from."""

    name: Nonempty
    value: int | None
    provenance: EnumProvenance
    basis: Nonempty


class ModelCountedCall(_Frozen):
    pattern_id: str
    kind: EnumRecoveryKind
    tool_name: str
    target: str
    turn_index: int
    tokens: int | None
    provenance: EnumProvenance


class ModelArmCost(_Frozen):
    arm: Nonempty
    session_id: Nonempty
    boundary_uuid: Nonempty
    compaction_input: ModelCostLine
    compaction_output: ModelCostLine
    compaction: ModelCostLine
    post_size: ModelCostLine
    recovery: ModelCostLine
    net: ModelCostLine
    cost: ModelCostLine
    counted_calls: tuple[ModelCountedCall, ...] = ()
    window_turns: int
    turns_observed: int
    window_complete: bool


class ModelCostRatio(_Frozen):
    """``cost(mod) / cost(stock)`` with its provenance flag and perturbation bound."""

    ratio: float | None
    status: EnumRatioStatus
    estimated_lines: tuple[str, ...] = ()
    perturbation_fraction: float = PERTURBATION_FRACTION
    perturbed_upper_ratio: float | None = None
    safeguard_upper_bound: float | None = None
    safeguard: EnumSafeguard | None = None
    basis: Nonempty


class ModelCompactionCostResult(_Frozen):
    stock: ModelArmCost | None = None
    mod: ModelArmCost | None = None
    ratio: ModelCostRatio | None = None
