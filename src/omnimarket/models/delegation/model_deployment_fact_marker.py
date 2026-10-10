# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The deployment-fact field marker of the packaged routing configs (OMN-20287).

Routing decisions belong in a deployment's overlay, not in the config this
package ships (operator rulings 2026-09-26T14:31:29Z and 2026-09-26T15:24:56Z:
no lab configuration in any shipped repository; customers run this
architecture without access to our lab). A field of a packaged routing config
model that records such a decision carries the marker below in its
``json_schema_extra``::

    model_name: str | None = Field(
        default=None, json_schema_extra=deployment_fact(EnumDeploymentFactKind.MODEL_NAME)
    )

The marked fields ARE the field list: :func:`deployment_fact_fields` reads them
off the typed models, so an overlay that carries a deployment's choices and the
gate that refuses them in packaged files read the same list.

:data:`NEUTRAL_LOCAL_ONLY_DEFAULTS` is the declared set of values a packaged
file may carry in a marked field: the capability-named local backends and the
``local`` tier, which name no host, model, credential or vendor. Any other
value in a marked field of a packaged file is a deployment fact.

The walker also reports every key of a packaged file that its typed model does
not declare (:func:`undeclared_key_paths`), so a new field cannot carry a
deployment fact past the marker by never being declared.
"""

from __future__ import annotations

import types
from collections.abc import Iterator, Mapping
from typing import Any, Final, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field
from pydantic.fields import FieldInfo

from omnimarket.enums.enum_deployment_fact_kind import EnumDeploymentFactKind

#: The ``json_schema_extra`` key that marks a field as a deployment fact.
DEPLOYMENT_FACT_KEY: Final[str] = "deployment_fact"

#: A list element of a packaged file, in a field path.
LIST_SEGMENT: Final[str] = "[]"

#: Any key of a mapping of models, in a field path.
MAPPING_SEGMENT: Final[str] = "{}"

#: The joiner a routing order is rendered with, so one order is one value.
ORDER_JOINER: Final[str] = " > "

_LOCAL_BACKENDS: Final[frozenset[str]] = frozenset(
    {"local-coder", "local-heavy-reasoning", "local-embedding"}
)
_LOCAL_TIERS: Final[frozenset[str]] = frozenset({"local"})

#: Values a packaged file may carry in a marked field. A routing order is
#: neutral only when every member is: an order that names one metered tier or
#: one vendor backend is that deployment's choice.
NEUTRAL_LOCAL_ONLY_DEFAULTS: Final[Mapping[EnumDeploymentFactKind, frozenset[str]]] = (
    types.MappingProxyType(
        {
            EnumDeploymentFactKind.BACKEND: _LOCAL_BACKENDS,
            EnumDeploymentFactKind.PROVIDER: frozenset({"local"}),
            EnumDeploymentFactKind.ENDPOINT: frozenset(),
            EnumDeploymentFactKind.MODEL_NAME: frozenset(),
            EnumDeploymentFactKind.SECRET_REF: frozenset(),
            EnumDeploymentFactKind.ROUTING_TIER: _LOCAL_TIERS,
            EnumDeploymentFactKind.ROUTING_ORDER: _LOCAL_BACKENDS | _LOCAL_TIERS,
        }
    )
)


def deployment_fact(kind: EnumDeploymentFactKind) -> dict[str, Any]:
    """The ``json_schema_extra`` that marks a field as a deployment fact of ``kind``."""
    return {DEPLOYMENT_FACT_KEY: kind.value}


def deployment_fact_kind(field: FieldInfo) -> EnumDeploymentFactKind | None:
    """The deployment-fact kind a field is marked with, or None when unmarked."""
    extra = field.json_schema_extra
    if not isinstance(extra, Mapping):
        return None
    raw = extra.get(DEPLOYMENT_FACT_KEY)
    return None if raw is None else EnumDeploymentFactKind(str(raw))


class ModelDeploymentFactField(BaseModel):
    """One marked field of a packaged routing config model, by path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: tuple[str, ...] = Field(
        ...,
        min_length=1,
        description=(
            "Keys from the file root to the field. ``[]`` stands for each list "
            "element and ``{}`` for each key of a mapping of models."
        ),
    )
    kind: EnumDeploymentFactKind

    @property
    def dotted(self) -> str:
        return render_path(self.path)


class ModelDeploymentFactOccurrence(BaseModel):
    """One value a packaged file carries in a marked field."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_name: str = Field(..., min_length=1)
    path: str = Field(
        ...,
        min_length=1,
        description="Concrete path: mapping keys are named, list elements are ``[]``.",
    )
    kind: EnumDeploymentFactKind
    value: str = Field(..., min_length=1)

    @property
    def neutral(self) -> bool:
        return is_neutral(self.kind, self.value)


def render_path(path: tuple[str, ...]) -> str:
    out = ""
    for segment in path:
        if segment == LIST_SEGMENT:
            out += LIST_SEGMENT
        else:
            out += segment if not out else f".{segment}"
    return out


def is_neutral(kind: EnumDeploymentFactKind, value: str) -> bool:
    """Whether ``value`` is in the declared neutral local-only default set for ``kind``."""
    neutral = NEUTRAL_LOCAL_ONLY_DEFAULTS[kind]
    if kind is EnumDeploymentFactKind.ROUTING_ORDER:
        return all(member in neutral for member in value.split(ORDER_JOINER))
    return value in neutral


def _unwrap_optional(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(members) == 1:
            return members[0]
    return annotation


def _model_class(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _container(annotation: Any) -> tuple[str, Any] | None:
    """``([] , element)`` for a list or tuple, ``({}, value)`` for a str-keyed mapping."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (list, tuple) and args:
        return LIST_SEGMENT, args[0]
    if origin is dict and len(args) == 2:
        return MAPPING_SEGMENT, args[1]
    return None


def deployment_fact_fields(
    model: type[BaseModel], prefix: tuple[str, ...] = ()
) -> tuple[ModelDeploymentFactField, ...]:
    """Every marked field reachable from ``model``, with its path from the file root."""
    found: list[ModelDeploymentFactField] = []
    for name, field in model.model_fields.items():
        path = (*prefix, name)
        kind = deployment_fact_kind(field)
        if kind is not None:
            found.append(ModelDeploymentFactField(path=path, kind=kind))
            continue
        annotation = _unwrap_optional(field.annotation)
        container = _container(annotation)
        if container is not None:
            segment, inner = container
            nested = _model_class(_unwrap_optional(inner))
            if nested is not None:
                found.extend(deployment_fact_fields(nested, (*path, segment)))
            continue
        nested = _model_class(annotation)
        if nested is not None:
            found.extend(deployment_fact_fields(nested, path))
    return tuple(found)


def _leaf_values(
    kind: EnumDeploymentFactKind, raw: Any, concrete: str
) -> Iterator[tuple[str, str]]:
    if raw is None:
        return
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            yield from _leaf_values(kind, value, f"{concrete}.{key}")
        return
    if isinstance(raw, list | tuple):
        members = [str(member) for member in raw if member is not None]
        if not members:
            return
        if kind is EnumDeploymentFactKind.ROUTING_ORDER:
            yield concrete, ORDER_JOINER.join(members)
            return
        for member in members:
            yield f"{concrete}{LIST_SEGMENT}", member
        return
    text = str(raw)
    if text:
        yield concrete, text


def _walk(raw: Any, path: tuple[str, ...], concrete: str) -> Iterator[tuple[str, Any]]:
    """The raw values at ``path`` under ``raw``, with their concrete paths."""
    if not path:
        yield concrete, raw
        return
    head, rest = path[0], path[1:]
    if head == LIST_SEGMENT:
        if isinstance(raw, list):
            for element in raw:
                yield from _walk(element, rest, f"{concrete}{LIST_SEGMENT}")
        return
    if head == MAPPING_SEGMENT:
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                yield from _walk(value, rest, f"{concrete}.{key}")
        return
    if isinstance(raw, Mapping) and head in raw:
        yield from _walk(
            raw[head], rest, head if not concrete else f"{concrete}.{head}"
        )


def extract_deployment_facts(
    file_name: str, raw: Mapping[str, Any], model: type[BaseModel]
) -> tuple[ModelDeploymentFactOccurrence, ...]:
    """Every value ``raw`` carries in a field ``model`` marks, neutral ones included."""
    found: list[ModelDeploymentFactOccurrence] = []
    for field in deployment_fact_fields(model):
        for concrete, value in _walk(raw, field.path, ""):
            for leaf_path, text in _leaf_values(field.kind, value, concrete):
                found.append(
                    ModelDeploymentFactOccurrence(
                        file_name=file_name,
                        path=leaf_path,
                        kind=field.kind,
                        value=text,
                    )
                )
    return tuple(found)


def _declared_keys(model: type[BaseModel]) -> frozenset[str]:
    keys: set[str] = set()
    for name, field in model.model_fields.items():
        keys.add(name)
        if field.alias:
            keys.add(field.alias)
    return frozenset(keys)


def undeclared_key_paths(
    raw: Any, model: type[BaseModel], prefix: tuple[str, ...] = ()
) -> frozenset[str]:
    """Every key under ``raw`` that ``model`` does not declare, as a path pattern.

    Lists are ``[]`` and mapping keys ``{}``, so a path names a field shape, not
    one row: a new task class carrying only declared fields adds no path, and a
    new key on any class does. An undeclared key's own value is not descended.
    """
    if not isinstance(raw, Mapping):
        return frozenset()
    found: set[str] = set()
    declared = _declared_keys(model)
    for key, value in raw.items():
        path = (*prefix, str(key))
        if str(key) not in declared:
            found.add(render_path(path))
            continue
        field = model.model_fields.get(str(key))
        if field is None or deployment_fact_kind(field) is not None:
            continue
        annotation = _unwrap_optional(field.annotation)
        container = _container(annotation)
        if container is not None:
            segment, inner = container
            nested = _model_class(_unwrap_optional(inner))
            if nested is None:
                continue
            if segment == LIST_SEGMENT and isinstance(value, list):
                for element in value:
                    found |= undeclared_key_paths(element, nested, (*path, segment))
            elif segment == MAPPING_SEGMENT and isinstance(value, Mapping):
                for element in value.values():
                    found |= undeclared_key_paths(element, nested, (*path, segment))
            continue
        nested = _model_class(annotation)
        if nested is not None:
            found |= undeclared_key_paths(value, nested, path)
    return frozenset(found)


__all__: list[str] = [
    "DEPLOYMENT_FACT_KEY",
    "NEUTRAL_LOCAL_ONLY_DEFAULTS",
    "ORDER_JOINER",
    "ModelDeploymentFactField",
    "ModelDeploymentFactOccurrence",
    "deployment_fact",
    "deployment_fact_fields",
    "deployment_fact_kind",
    "extract_deployment_facts",
    "is_neutral",
    "render_path",
    "undeclared_key_paths",
]
