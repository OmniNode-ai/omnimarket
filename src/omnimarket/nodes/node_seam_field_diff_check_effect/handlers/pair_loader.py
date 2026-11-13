# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve every producer-consumer contract pair to its two declared models (OMN-24121).

STANDALONE ON PURPOSE. This module imports only the standard library, ``yaml`` and
``pydantic``, never anything from ``omnimarket``: the check also runs it as a script
(``python runtime_pair_loader.py --root PACKAGE=PATH ...``, which loads this module) against a base revision's source
tree, where this node does not exist yet. The models it imports are the ones on
``sys.path``, so the base run reads the base revision's own models.

Pair resolution uses only the tiers that name the model the runtime itself validates
into, never a guess from a word shared by a model name and a topic:

* producer: R1 a handler route on the topic with an ``output_model``; R2 a
  ``published_events`` row whose event type is a handler output model's class name;
  R3 one distinct output model and one publish topic; R5 one handler ``handle()``
  return model and one publish topic.
* consumer: C1 a handler route on the topic with an ``event_model``; C2 one distinct
  ``event_model`` and one subscribe topic; C6 one handler ``handle()`` first-parameter
  model and one subscribe topic (the model an ``operation_match`` route validates the
  payload into); C3 the contract's ``input_model`` and one subscribe topic.

A pair whose consumer model is a generic transport envelope is never a seam and is
skipped. A model that cannot be imported lands in ``unreadable`` with its reason; a
pair is never dropped silently.
"""

from __future__ import annotations

import importlib
import inspect
import re
import typing
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel

_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_EXCLUDED_PARTS: tuple[str, ...] = ("/tests/", "/.venv/")
_ADAPTER_MODES: frozenset[str] = frozenset({"before", "wrap"})

ModelRef = tuple[str, str]


def _model_ref(value: object) -> ModelRef | None:
    """A (module, class) pair from a contract's model reference, or None."""
    if isinstance(value, dict) and value.get("name") and value.get("module"):
        return (str(value["module"]), str(value["name"]))
    if isinstance(value, str) and "." in value:
        module, _, name = value.rpartition(".")
        return (module, name)
    return None


def _normalised(name: str) -> str:
    return re.sub(r"^model", "", re.sub(r"[^a-z0-9]", "", name.lower()))


def _load_contracts(
    package_roots: Mapping[str, Path],
) -> dict[str, tuple[str, dict[str, object]]]:
    """Every contract.yaml under every package root, keyed by '<package>/<relative path>'."""
    contracts: dict[str, tuple[str, dict[str, object]]] = {}
    for package, root in package_roots.items():
        for path in sorted(root.rglob("contract.yaml")):
            relative = f"{package}/{path.relative_to(root).as_posix()}"
            if any(part in f"/{relative}" for part in _EXCLUDED_PARTS):
                continue
            try:
                loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=_LOADER)
            except (OSError, yaml.YAMLError):
                continue
            if isinstance(loaded, dict):
                contracts[relative] = (package, loaded)
    return contracts


def _routes(contract: Mapping[str, object]) -> list[dict[str, object]]:
    routing = contract.get("handler_routing")
    handlers = routing.get("handlers") if isinstance(routing, dict) else None
    return [h for h in handlers or [] if isinstance(h, dict)]


def _topics(contract: Mapping[str, object], key: str) -> list[str]:
    bus = contract.get("event_bus")
    topics = bus.get(key) if isinstance(bus, dict) else None
    return [t for t in topics or [] if isinstance(t, str)]


def _is_model_class(candidate: object) -> bool:
    return (
        inspect.isclass(candidate)
        and issubclass(candidate, BaseModel)
        and candidate is not BaseModel
    )


def _handler_signature(
    contract: Mapping[str, object],
    cache: dict[ModelRef, tuple[ModelRef | None, ModelRef | None]],
) -> tuple[set[ModelRef], set[ModelRef]]:
    """(first-parameter models, return models) of the handlers' ``handle()`` methods."""
    specs: list[ModelRef] = []
    for route in _routes(contract):
        handler = route.get("handler")
        if isinstance(handler, dict) and handler.get("module") and handler.get("name"):
            specs.append((str(handler["module"]), str(handler["name"])))
    routing = contract.get("handler_routing")
    default = routing.get("default_handler") if isinstance(routing, dict) else None
    if isinstance(default, str) and ":" in default:
        module, _, name = default.partition(":")
        specs.append((module, name))
    inputs: set[ModelRef] = set()
    outputs: set[ModelRef] = set()
    for spec in specs:
        if spec not in cache:
            cache[spec] = _read_handle_signature(spec)
        first, returned = cache[spec]
        if first:
            inputs.add(first)
        if returned:
            outputs.add(returned)
    return inputs, outputs


def _read_handle_signature(spec: ModelRef) -> tuple[ModelRef | None, ModelRef | None]:
    try:
        cls = getattr(importlib.import_module(spec[0]), spec[1])
        handle = getattr(cls, "handle", None)
        if handle is None:
            return (None, None)
        hints = typing.get_type_hints(handle)
        parameters = [
            name
            for name in inspect.signature(handle).parameters
            if name not in ("self", "cls")
        ]
        first = hints.get(parameters[0]) if parameters else None
        returned = hints.get("return")
    except Exception:
        return (None, None)

    def reference(candidate: object) -> ModelRef | None:
        if _is_model_class(candidate):
            cls_ = typing.cast("type[BaseModel]", candidate)
            return (cls_.__module__, cls_.__name__)
        return None

    return (reference(first), reference(returned))


def _producer_model(
    contract: Mapping[str, object],
    topic: str,
    cache: dict[ModelRef, tuple[ModelRef | None, ModelRef | None]],
) -> tuple[ModelRef, str] | None:
    publish = _topics(contract, "publish_topics")
    routes = _routes(contract)
    handler_outputs = {m for r in routes if (m := _model_ref(r.get("output_model")))}
    top = _model_ref(contract.get("output_model"))
    every_output = handler_outputs | ({top} if top else set())
    for route in routes:
        if route.get("topic") == topic and (m := _model_ref(route.get("output_model"))):
            return m, "R1"
    published = contract.get("published_events")
    for event in published if isinstance(published, list) else []:
        if (
            isinstance(event, dict)
            and event.get("topic") == topic
            and event.get("event_type")
        ):
            for model in sorted(every_output):
                if _normalised(model[1]) == _normalised(str(event["event_type"])):
                    return model, "R2"
    if len(handler_outputs) == 1 and len(publish) == 1:
        return next(iter(handler_outputs)), "R3"
    if len(every_output) == 1 and len(publish) == 1:
        return next(iter(every_output)), "R3"
    _, returned = _handler_signature(contract, cache)
    if len(returned) == 1 and len(publish) == 1:
        return next(iter(returned)), "R5"
    return None


def _consumer_model(
    contract: Mapping[str, object],
    topic: str,
    cache: dict[ModelRef, tuple[ModelRef | None, ModelRef | None]],
) -> tuple[ModelRef, str] | None:
    subscribe = _topics(contract, "subscribe_topics")
    routes = _routes(contract)
    for route in routes:
        if route.get("topic") == topic and (m := _model_ref(route.get("event_model"))):
            return m, "C1"
    event_models = {m for r in routes if (m := _model_ref(r.get("event_model")))}
    if len(event_models) == 1 and subscribe == [topic]:
        return next(iter(event_models)), "C2"
    inputs, _ = _handler_signature(contract, cache)
    if len(inputs) == 1 and len(subscribe) == 1:
        return next(iter(inputs)), "C6"
    declared = _model_ref(contract.get("input_model"))
    if declared and subscribe == [topic]:
        return declared, "C3"
    return None


def _normalise_type(schema: Mapping[str, object], defs: Mapping[str, object]) -> str:
    """The type vocabulary the field comparison reads (see HandlerSeamFieldDiff)."""
    ref = schema.get("$ref")
    if isinstance(ref, str):
        name = ref.split("/")[-1]
        target = defs.get(name)
        if isinstance(target, dict) and "enum" in target:
            return "enum:" + ",".join(sorted(map(str, target["enum"])))
        return f"ref:{name}"
    for key in ("anyOf", "oneOf"):
        parts = schema.get(key)
        if isinstance(parts, list):
            return "|".join(
                sorted({_normalise_type(p, defs) for p in parts if isinstance(p, dict)})
            )
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        return "&".join(
            sorted(_normalise_type(p, defs) for p in all_of if isinstance(p, dict))
        )
    if "enum" in schema and isinstance(schema["enum"], list):
        return "enum:" + ",".join(sorted(map(str, schema["enum"])))
    if "const" in schema:
        return f"const:{schema['const']}"
    kind = schema.get("type")
    if kind == "array":
        items = schema.get("items")
        return (
            "array<"
            + _normalise_type(items if isinstance(items, dict) else {}, defs)
            + ">"
        )
    if kind == "object":
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            return "map<" + _normalise_type(extra, defs) + ">"
        return "object"
    if isinstance(kind, str):
        fmt = schema.get("format")
        return f"{kind}:{fmt}" if isinstance(fmt, str) else kind
    return "any"


def _fields(
    properties: Mapping[str, object],
    required: set[str],
    defs: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    facts: dict[str, dict[str, object]] = {}
    for name, raw in properties.items():
        schema = raw if isinstance(raw, dict) else {}
        type_name = _normalise_type(schema, defs)
        parts = type_name.split("|")
        facts[name] = {
            "type_name": "|".join(p for p in parts if p != "null") or "any",
            "required": name in required,
            "nullable": "null" in parts,
            "has_default": schema.get("default") is not None,
        }
    return facts


def summarize_model(cls: type[BaseModel]) -> dict[str, object]:
    """The JSON of a ModelSeamModelSummary for one pydantic model class."""
    schema = cls.model_json_schema()
    defs = schema.get("$defs", {})
    properties = schema.get("properties", {})
    nested: dict[str, dict[str, dict[str, object]]] = {}
    for name, definition in defs.items():
        if isinstance(definition, dict) and "properties" in definition:
            nested[name] = _fields(
                definition["properties"], set(definition.get("required", [])), defs
            )
    decorators = getattr(cls, "__pydantic_decorators__", None)
    validators = getattr(decorators, "model_validators", {}) or {}
    return {
        "model_ref": f"{cls.__module__}.{cls.__name__}",
        "fields": _fields(properties, set(schema.get("required", [])), defs),
        "nested": nested,
        "extra_forbidden": schema.get("additionalProperties") is False,
        "has_adapter": any(
            getattr(v.info, "mode", None) in _ADAPTER_MODES for v in validators.values()
        ),
    }


def _import_model(ref: ModelRef) -> type[BaseModel]:
    loaded = getattr(importlib.import_module(ref[0]), ref[1])
    if not _is_model_class(loaded):
        raise TypeError(f"{ref[0]}.{ref[1]} is not a pydantic model")
    return typing.cast("type[BaseModel]", loaded)


def collect_pairs(
    package_roots: Mapping[str, Path], envelope_models: Sequence[str]
) -> dict[str, object]:
    """Every resolvable (topic, producer, consumer) pair and what could not be read."""
    contracts = _load_contracts(package_roots)
    published: dict[str, list[str]] = {}
    subscribed: dict[str, list[str]] = {}
    for path, (_, contract) in contracts.items():
        for topic in _topics(contract, "publish_topics"):
            published.setdefault(topic, []).append(path)
        for topic in _topics(contract, "subscribe_topics"):
            subscribed.setdefault(topic, []).append(path)
    signature_cache: dict[ModelRef, tuple[ModelRef | None, ModelRef | None]] = {}
    summaries: dict[ModelRef, dict[str, object]] = {}
    unreadable: list[str] = []
    pairs: list[dict[str, object]] = []

    def summary(ref: ModelRef, where: str) -> dict[str, object] | None:
        if ref not in summaries:
            try:
                summaries[ref] = summarize_model(_import_model(ref))
            except Exception as error:
                unreadable.append(
                    f"{where}: {ref[0]}.{ref[1]}: {type(error).__name__}: "
                    f"{str(error)[:120]}"
                )
                return None
        return summaries[ref]

    for topic in sorted(published):
        for producer_path in published[topic]:
            for consumer_path in subscribed.get(topic, []):
                if producer_path == consumer_path:
                    continue
                produced = _producer_model(
                    contracts[producer_path][1], topic, signature_cache
                )
                consumed = _consumer_model(
                    contracts[consumer_path][1], topic, signature_cache
                )
                if produced is None or consumed is None:
                    continue
                if f"{consumed[0][0]}.{consumed[0][1]}" in envelope_models:
                    continue
                where = f"{topic} {producer_path} -> {consumer_path}"
                producer = summary(produced[0], where)
                consumer = summary(consumed[0], where)
                if producer is None or consumer is None:
                    continue
                pairs.append(
                    {
                        "topic": topic,
                        "producer_contract": producer_path,
                        "consumer_contract": consumer_path,
                        "producer_rule": produced[1],
                        "consumer_rule": consumed[1],
                        "producer": producer,
                        "consumer": consumer,
                    }
                )
    return {"pairs": pairs, "unreadable": sorted(set(unreadable))}
