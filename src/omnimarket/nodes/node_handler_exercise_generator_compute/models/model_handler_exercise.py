# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, generated events and skipped rows of the graph-generated handler exercise (OMN-26337)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_code_graph_handlers import ModelHandlerRow


class EnumPayloadSource(StrEnum):
    """Where an event's payload came from."""

    #: A payload the contract declares as an example of the operation.
    CONTRACT_EXAMPLE = "CONTRACT_EXAMPLE"
    #: A minimal payload built from the typed input model's own schema.
    INPUT_MODEL_SCHEMA = "INPUT_MODEL_SCHEMA"


class EnumNoEventReason(StrEnum):
    """Why a graph row yielded no event. A row is never dropped: it is an event or one of these."""

    UNRESOLVED_REFERENCE = "unresolved-reference"
    #: The graph row carries no input model (the contract declares it, if at all, outside the operation entry).
    INPUT_MODEL_NOT_DECLARED = "input-model-not-declared"
    NO_ROUTE = "no-route"
    INPUT_MODEL_NOT_IMPORTABLE = "input-model-not-importable"
    INPUT_MODEL_NOT_A_MODEL = "input-model-not-a-model"
    PAYLOAD_NOT_BUILDABLE = "payload-not-buildable"


class ModelHandlerExerciseRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelHandlerRow, ...]
    #: The deadline every event carries; the pass rule keeps it.
    deadline_ms: int = Field(default=30_000, gt=0)


class ModelHandlerExerciseEvent(BaseModel):
    """One event for one handler input, addressed to the operation's route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    handler: str = Field(min_length=1)
    input_model: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    payload: dict[str, object]
    payload_source: EnumPayloadSource
    #: Stable across runs: contract, operation, handler class and input model, so a rerun addresses the same event.
    event_id: str = Field(min_length=1)
    deadline_ms: int = Field(gt=0)


class ModelHandlerWithoutEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: str
    operation: str
    reason: EnumNoEventReason
    detail: str = ""


class ModelHandlerExerciseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    events: tuple[ModelHandlerExerciseEvent, ...]
    without_event: tuple[ModelHandlerWithoutEvent, ...]
