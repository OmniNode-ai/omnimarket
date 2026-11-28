# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Graph rows -> one exercise event per handler input (OMN-26337, AC1).

Every row of the code graph's ``handlers`` question becomes exactly one event or one named
reason it has none; no row is dropped and no handler is listed by hand. The payload is the
operation's first declared contract example when it validates against the typed input model,
otherwise a minimal payload built from the model's own JSON schema and validated by the model.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import cast

from pydantic import BaseModel, ValidationError

from omnimarket.models.model_code_graph_handlers import (
    EnumHandlerReference,
    EnumHandlerRoute,
    EnumUnresolvedReason,
    ModelHandlerRow,
)
from omnimarket.nodes.node_handler_exercise_generator_compute.models.model_handler_exercise import (
    EnumNoEventReason,
    EnumPayloadSource,
    ModelHandlerExerciseEvent,
    ModelHandlerExerciseRequest,
    ModelHandlerExerciseResult,
    ModelHandlerWithoutEvent,
)

_NIL_UUID = "00000000-0000-0000-0000-000000000000"
_EPOCH = "1970-01-01T00:00:00Z"
_MAX_DEPTH = 6


def _resolve(
    schema: Mapping[str, object], defs: Mapping[str, object]
) -> Mapping[str, object]:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = defs.get(ref.rsplit("/", 1)[-1])
        if isinstance(target, dict):
            return cast("Mapping[str, object]", target)
    return schema


def _minimal(
    schema: Mapping[str, object], defs: Mapping[str, object], depth: int
) -> object:
    """The smallest value the JSON schema admits; defaults and constants win."""
    schema = _resolve(schema, defs)
    if "default" in schema:
        return schema["default"]
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            non_null = [
                o for o in options if isinstance(o, dict) and o.get("type") != "null"
            ]
            pick = non_null[0] if non_null else options[0]
            return _minimal(cast("Mapping[str, object]", pick), defs, depth)
    all_of = schema.get("allOf")
    if isinstance(all_of, list) and all_of and isinstance(all_of[0], dict):
        return _minimal(cast("Mapping[str, object]", all_of[0]), defs, depth)
    kind = schema.get("type")
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "uuid":
            return _NIL_UUID
        if fmt == "date-time":
            return _EPOCH
        if fmt == "date":
            return "1970-01-01"
        return "x" * int(cast("int", schema.get("minLength", 1) or 1))
    if kind == "integer":
        return int(cast("int", schema.get("minimum", 0)))
    if kind == "number":
        return float(cast("float", schema.get("minimum", 0)))
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "null":
        return None
    if kind == "object" or "properties" in schema:
        if depth >= _MAX_DEPTH:
            return {}
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, dict) or not isinstance(required, list):
            return {}
        return {
            name: _minimal(
                cast("Mapping[str, object]", properties[name]), defs, depth + 1
            )
            for name in required
            if name in properties
        }
    return None


def _model_class(dotted: str) -> type[BaseModel] | str:
    module, _, name = dotted.rpartition(".")
    try:
        found = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as error:
        return f"{type(error).__name__}: {error}"
    if isinstance(found, type) and issubclass(found, BaseModel):
        return found
    return "not-a-pydantic-model"


def _payload(
    row: ModelHandlerRow, model: type[BaseModel]
) -> tuple[dict[str, object], EnumPayloadSource] | str:
    for example in row.examples:
        try:
            model.model_validate(example.payload)
        except ValidationError:
            continue
        return dict(example.payload), EnumPayloadSource.CONTRACT_EXAMPLE
    schema = model.model_json_schema()
    defs = cast("Mapping[str, object]", schema.get("$defs", {}))
    built = _minimal(schema, defs, 0)
    if not isinstance(built, dict):
        return "schema-is-not-an-object"
    try:
        model.model_validate(built)
    except ValidationError as error:
        return f"built payload refused by the model: {error.error_count()} error(s)"
    return built, EnumPayloadSource.INPUT_MODEL_SCHEMA


def _without(
    row: ModelHandlerRow, reason: EnumNoEventReason, detail: str = ""
) -> ModelHandlerWithoutEvent:
    return ModelHandlerWithoutEvent(
        contract=row.contract, operation=row.operation, reason=reason, detail=detail
    )


class HandlerHandlerExerciseGenerator:
    """Compute handler: handler rows in, exercise events and named gaps out."""

    def handle(
        self, request: ModelHandlerExerciseRequest
    ) -> ModelHandlerExerciseResult:
        events: list[ModelHandlerExerciseEvent] = []
        gaps: list[ModelHandlerWithoutEvent] = []
        for row in request.rows:
            # An unresolved output model does not stop an event: only the handler and its input matter.
            blocking = [
                u
                for u in row.unresolved
                if u.reference is not EnumHandlerReference.OUTPUT_MODEL
            ]
            if blocking:
                undeclared = all(
                    u.reason is EnumUnresolvedReason.NOT_DECLARED for u in blocking
                ) and all(
                    u.reference is EnumHandlerReference.INPUT_MODEL for u in blocking
                )
                gaps.append(
                    _without(
                        row,
                        EnumNoEventReason.INPUT_MODEL_NOT_DECLARED
                        if undeclared
                        else EnumNoEventReason.UNRESOLVED_REFERENCE,
                        ",".join(
                            f"{u.reference.value}:{u.reason.value}" for u in blocking
                        ),
                    )
                )
                continue
            topic = row.command_topic or (
                row.subscribe_topics[0] if row.subscribe_topics else None
            )
            if row.route is EnumHandlerRoute.NO_ROUTE or topic is None:
                gaps.append(_without(row, EnumNoEventReason.NO_ROUTE))
                continue
            model = _model_class(row.input_model)
            if isinstance(model, str):
                reason = (
                    EnumNoEventReason.INPUT_MODEL_NOT_A_MODEL
                    if model == "not-a-pydantic-model"
                    else EnumNoEventReason.INPUT_MODEL_NOT_IMPORTABLE
                )
                gaps.append(_without(row, reason, model))
                continue
            built = _payload(row, model)
            if isinstance(built, str):
                gaps.append(
                    _without(row, EnumNoEventReason.PAYLOAD_NOT_BUILDABLE, built)
                )
                continue
            payload, source = built
            events.append(
                ModelHandlerExerciseEvent(
                    contract=row.contract,
                    operation=row.operation,
                    handler=f"{row.handler_module}.{row.handler_class}",
                    input_model=row.input_model,
                    topic=topic,
                    payload=payload,
                    payload_source=source,
                    event_id=f"{row.contract}::{row.operation}::{row.handler_class}::{row.input_model}::{topic}",
                    deadline_ms=request.deadline_ms,
                )
            )
        return ModelHandlerExerciseResult(
            events=tuple(events), without_event=tuple(gaps)
        )
