# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Grade bus-model seams between producers and consumers."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AliasChoices

from ..models.model_bus_model_seam_check_request import ModelBusModelSeamCheckRequest
from ..models.model_bus_model_seam_check_result import ModelBusModelSeamCheckResult
from ..models.model_bus_model_seam_external_producer import (
    ModelBusModelSeamExternalProducer,
)
from ..models.model_bus_model_seam_pair import ModelBusModelSeamPair
from ..models.model_bus_model_seam_pin import ModelBusModelSeamPin
from ..models.model_bus_model_seam_policy import ModelBusModelSeamPolicy


def _build_class_index(root: Path, package: str) -> dict[str, list[str]]:
    """Map class names to module paths for top-level Model* classes."""
    index: dict[str, list[str]] = {}
    src_dir = root / "src" / package
    if not src_dir.is_dir():
        return index
    for py_file in sorted(src_dir.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        module_path = (
            py_file.relative_to(root / "src")
            .with_suffix("")
            .as_posix()
            .replace("/", ".")
        )
        if module_path.endswith(".__init__"):
            module_path = module_path[: -len(".__init__")]
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.startswith("Model"):
                index.setdefault(node.name, []).append(module_path)
    return index


def _load_contracts(root: Path, package: str) -> list[tuple[str, dict[str, Any]]]:
    """Load every contract.yaml under src/<package>/nodes/*/."""
    contracts: list[tuple[str, dict[str, Any]]] = []
    nodes_dir = root / "src" / package / "nodes"
    if not nodes_dir.is_dir():
        return contracts
    for contract_path in sorted(nodes_dir.glob("*/contract.yaml")):
        try:
            data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        node_name = contract_path.parent.name
        contracts.append((node_name, data))
    return contracts


def _resolve_model(
    name: str,
    index: dict[str, list[str]],
    preferred_prefix: str,
) -> str | None:
    """Resolve a class name to a fully qualified model path."""
    modules = index.get(name, [])
    preferred = [m for m in modules if m.startswith(preferred_prefix)]
    if len(preferred) == 1:
        return f"{preferred[0]}.{name}"
    if len(modules) == 1:
        return f"{modules[0]}.{name}"
    return None


def _extract_producers(
    node_name: str,
    contract: dict[str, Any],
    index: dict[str, list[str]],
    package: str,
) -> list[tuple[str, str, str, str]]:
    """Extract (node, model, topic, source) producers from a contract."""
    producers: list[tuple[str, str, str, str]] = []
    preferred_prefix = f"{package}.nodes.{node_name}"

    published = contract.get("published_events")
    if isinstance(published, list):
        for entry in published:
            if not isinstance(entry, dict):
                continue
            topic = entry.get("topic")
            event_type = entry.get("event_type")
            if not isinstance(topic, str) or not isinstance(event_type, str):
                continue
            fq = _resolve_model(event_type, index, preferred_prefix)
            if fq is None:
                continue
            producers.append((node_name, fq, topic, "explicit"))

    terminal_event = contract.get("terminal_event")
    if isinstance(terminal_event, str):
        output_model = contract.get("output_model")
        if (
            isinstance(output_model, dict)
            and "module" in output_model
            and "name" in output_model
        ):
            fq = f"{output_model['module']}.{output_model['name']}"
            producers.append((node_name, fq, terminal_event, "inferred"))
        else:
            handler = contract.get("handler", {})
            if isinstance(handler, dict):
                handler_output = handler.get("output_model")
                if isinstance(handler_output, str) and "." in handler_output:
                    producers.append(
                        (node_name, handler_output, terminal_event, "inferred")
                    )

    return producers


def _extract_consumers(
    node_name: str,
    contract: dict[str, Any],
    index: dict[str, list[str]],
    package: str,
) -> list[tuple[str, str, str, str]]:
    """Extract (node, model, topic, source) consumers from a contract."""
    consumers: list[tuple[str, str, str, str]] = []

    routing = contract.get("handler_routing", {})
    if isinstance(routing, dict):
        handlers = routing.get("handlers", [])
        if isinstance(handlers, list):
            for h in handlers:
                if not isinstance(h, dict):
                    continue
                topic = h.get("topic")
                if not isinstance(topic, str):
                    continue
                event_model = h.get("event_model")
                if (
                    isinstance(event_model, dict)
                    and "module" in event_model
                    and "name" in event_model
                ):
                    fq = f"{event_model['module']}.{event_model['name']}"
                    consumers.append((node_name, fq, topic, "explicit"))
                    continue
                input_model = h.get("input_model")
                if isinstance(input_model, str) and "." in input_model:
                    consumers.append((node_name, input_model, topic, "explicit"))
                    continue
                # Fall back to contract-level input_model
                c_input = contract.get("input_model")
                if (
                    isinstance(c_input, dict)
                    and "module" in c_input
                    and "name" in c_input
                ):
                    fq = f"{c_input['module']}.{c_input['name']}"
                    consumers.append((node_name, fq, topic, "inferred"))
                else:
                    handler = contract.get("handler", {})
                    if isinstance(handler, dict):
                        h_input = handler.get("input_model")
                        if isinstance(h_input, str) and "." in h_input:
                            consumers.append((node_name, h_input, topic, "inferred"))

    # Single subscribe_topics entry not covered by routing
    event_bus = contract.get("event_bus", {})
    if isinstance(event_bus, dict):
        subs = event_bus.get("subscribe_topics", [])
        if isinstance(subs, list) and len(subs) == 1:
            topic = subs[0]
            if isinstance(topic, str):
                covered = False
                routing = contract.get("handler_routing", {})
                if isinstance(routing, dict):
                    handlers = routing.get("handlers", [])
                    if isinstance(handlers, list):
                        for h in handlers:
                            if isinstance(h, dict) and h.get("topic") == topic:
                                covered = True
                                break
                if not covered:
                    c_input = contract.get("input_model")
                    if (
                        isinstance(c_input, dict)
                        and "module" in c_input
                        and "name" in c_input
                    ):
                        fq = f"{c_input['module']}.{c_input['name']}"
                        consumers.append((node_name, fq, topic, "inferred"))
                    else:
                        handler = contract.get("handler", {})
                        if isinstance(handler, dict):
                            h_input = handler.get("input_model")
                            if isinstance(h_input, str) and "." in h_input:
                                consumers.append(
                                    (node_name, h_input, topic, "inferred")
                                )

    return consumers


def _field_accepted_names(field_name: str, field: Any) -> set[str]:
    """Every key a field accepts: its name, its alias and its validation aliases."""
    names = {field_name}
    if field.alias:
        names.add(field.alias)
    validation_alias = field.validation_alias
    if isinstance(validation_alias, str):
        names.add(validation_alias)
    elif isinstance(validation_alias, AliasChoices):
        names.update(c for c in validation_alias.choices if isinstance(c, str))
    return names


def _emit_names(model_cls: type) -> set[str]:
    """Get the set of names a producer model emits."""
    names: set[str] = set()
    for field_name, field in model_cls.model_fields.items():  # type: ignore[attr-defined]
        names.add(field.serialization_alias or field.alias or field_name)
    return names


def _accept_names(model_cls: type) -> set[str]:
    """Get the set of names a consumer model accepts."""
    names: set[str] = set()
    for field_name, field in model_cls.model_fields.items():  # type: ignore[attr-defined]
        names |= _field_accepted_names(field_name, field)
    return names


def _load_model_class(fq: str) -> tuple[type | None, str]:
    """Load a pydantic model class by fully qualified path; the text names a failure."""
    module_name, _, class_name = fq.rpartition(".")
    if not module_name or not class_name:
        return None, f"{fq}: not a module path and class name"
    try:
        module = importlib.import_module(module_name)
        return getattr(module, class_name), ""
    except Exception as exc:
        return None, f"{fq}: {exc!r}"


def _grade_pair(
    producer_fq: str,
    consumer_fq: str,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """Grade a producer/consumer pair. Returns (refused, missing, unresolved)."""
    if producer_fq == consumer_fq:
        return (), (), ""

    producer_cls, producer_error = _load_model_class(producer_fq)
    consumer_cls, consumer_error = _load_model_class(consumer_fq)
    if producer_cls is None or consumer_cls is None:
        return (), (), "; ".join(e for e in (producer_error, consumer_error) if e)

    emit = _emit_names(producer_cls)
    accept = _accept_names(consumer_cls)

    refused: tuple[str, ...] = ()
    if getattr(consumer_cls, "model_config", {}).get("extra") == "forbid":
        refused = tuple(sorted(emit - accept))

    missing = tuple(
        sorted(
            field_name
            for field_name, field in consumer_cls.model_fields.items()  # type: ignore[attr-defined]
            if field.is_required()
            and not _field_accepted_names(field_name, field) & emit
        )
    )

    return refused, missing, ""


def _load_policy(policy_path: Path) -> ModelBusModelSeamPolicy:
    """Load the seam policy YAML file."""
    data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        data = {}
    strict_models = tuple(data.get("strict_models", []))
    external_producers_raw = data.get("external_producers", [])
    external_producers = tuple(
        ModelBusModelSeamExternalProducer(**ep) for ep in external_producers_raw
    )
    pins_raw = data.get("pins", [])
    pins = tuple(ModelBusModelSeamPin(**p) for p in pins_raw)
    return ModelBusModelSeamPolicy(
        strict_models=strict_models,
        external_producers=external_producers,
        pins=pins,
    )


class HandlerBusModelSeamCheck:
    """Grade bus-model seams between producers and consumers."""

    def handle(
        self, request: ModelBusModelSeamCheckRequest
    ) -> ModelBusModelSeamCheckResult:
        root = Path(request.repo_root).resolve()
        package = request.package

        # Load policy
        if request.policy_path is not None:
            policy_path = Path(request.policy_path).resolve()
        else:
            policy_path = Path(__file__).parent.parent / "seam_policy.yaml"
        policy = _load_policy(policy_path)

        # Build class index
        index = _build_class_index(root, package)

        # Load contracts
        contracts = _load_contracts(root, package)

        # Build producer and consumer maps per topic
        producers_by_topic: dict[str, list[tuple[str, str, str]]] = {}
        consumers_by_topic: dict[str, list[tuple[str, str, str]]] = {}

        for node_name, contract in contracts:
            for prod in _extract_producers(node_name, contract, index, package):
                p_node, p_fq, p_topic, p_source = prod
                producers_by_topic.setdefault(p_topic, []).append(
                    (p_node, p_fq, p_source)
                )
            for cons in _extract_consumers(node_name, contract, index, package):
                c_node, c_fq, c_topic, c_source = cons
                consumers_by_topic.setdefault(c_topic, []).append(
                    (c_node, c_fq, c_source)
                )

        # Add external producers from policy
        for ep in policy.external_producers:
            producers_by_topic.setdefault(ep.topic, []).append(
                ("external", ep.model, "explicit")
            )

        # Build pairs
        pairs_by_key: dict[tuple[str, str, str, str, str], ModelBusModelSeamPair] = {}
        all_topics = set(producers_by_topic.keys()) & set(consumers_by_topic.keys())

        for topic in all_topics:
            for p_node, p_fq, p_source in producers_by_topic[topic]:
                for c_node, c_fq, c_source in consumers_by_topic[topic]:
                    refused, missing, unresolved = _grade_pair(p_fq, c_fq)
                    source: Literal["explicit", "inferred"] = (
                        "explicit"
                        if p_source == "explicit" and c_source == "explicit"
                        else "inferred"
                    )
                    pair_key = (topic, p_node, p_fq, c_node, c_fq)
                    known = pairs_by_key.get(pair_key)
                    if known is not None and known.source == "explicit":
                        continue
                    pairs_by_key[pair_key] = ModelBusModelSeamPair(
                        topic=topic,
                        producer_node=p_node,
                        producer_model=p_fq,
                        consumer_node=c_node,
                        consumer_model=c_fq,
                        source=source,
                        refused=refused,
                        missing=missing,
                        unresolved=unresolved,
                    )

        pairs = list(pairs_by_key.values())
        pairs.sort(key=lambda p: (p.topic, p.producer_model, p.consumer_model))

        # Grade pairs and produce verdicts
        output_lines: list[str] = []
        strict_failures: list[str] = []
        unpinned_pairs: list[ModelBusModelSeamPair] = []
        stale_pins: list[ModelBusModelSeamPin] = []
        pinned_pair_count = 0
        clean_pair_count = 0
        explicit_pair_count = 0
        inferred_pair_count = 0

        pin_map: dict[tuple[str, str, str], list[ModelBusModelSeamPin]] = {}
        for pin in policy.pins:
            key = (pin.topic, pin.producer_model, pin.consumer_model)
            pin_map.setdefault(key, []).append(pin)

        matched_pin_keys: set[tuple[str, str, str]] = set()

        for pair in pairs:
            if pair.source == "explicit":
                explicit_pair_count += 1
            else:
                inferred_pair_count += 1

            if pair.unresolved:
                output_lines.append(
                    f"UNRESOLVED {pair.topic} {pair.producer_model} -> {pair.consumer_model}: {pair.unresolved}"
                )
                continue

            if not pair.refused and not pair.missing:
                clean_pair_count += 1
                continue

            # Pair has refused or missing
            key = (pair.topic, pair.producer_model, pair.consumer_model)
            if pair.source == "explicit":
                # Both sides are stated outright by contracts: nothing can be pinned.
                unpinned_pairs.append(pair)
                output_lines.append(
                    f"REFUSED {pair.topic} {pair.producer_model} -> {pair.consumer_model}: "
                    f"refused={list(pair.refused)} missing={list(pair.missing)}"
                )
            else:
                # Inferred: check for matching pin
                pins = pin_map.get(key, [])
                matched = False
                for pin in pins:
                    if set(pin.refused) == set(pair.refused) and set(
                        pin.missing
                    ) == set(pair.missing):
                        matched = True
                        matched_pin_keys.add(key)
                        break
                if matched:
                    pinned_pair_count += 1
                else:
                    unpinned_pairs.append(pair)
                    output_lines.append(
                        f"UNPINNED {pair.topic} {pair.producer_model} -> {pair.consumer_model}: "
                        f"refused={list(pair.refused)} missing={list(pair.missing)}"
                    )

        # Check stale pins
        for pin in policy.pins:
            key = (pin.topic, pin.producer_model, pin.consumer_model)
            if key in matched_pin_keys:
                continue
            # Check if pin matches any pair with equal refused/missing
            pair_match = False
            for pair in pairs:
                pkey = (pair.topic, pair.producer_model, pair.consumer_model)
                if (
                    pkey == key
                    and set(pin.refused) == set(pair.refused)
                    and set(pin.missing) == set(pair.missing)
                ):
                    pair_match = True
                    matched_pin_keys.add(key)
                    break
            if not pair_match:
                stale_pins.append(pin)
                output_lines.append(
                    f"STALE PIN {pin.topic} {pin.producer_model} -> {pin.consumer_model}: "
                    f"refused={list(pin.refused)} missing={list(pin.missing)}"
                )

        # Strict model checks
        strict_models_set = set(policy.strict_models)
        pair_model_set: set[str] = set()
        for pair in pairs:
            pair_model_set.add(pair.producer_model)
            pair_model_set.add(pair.consumer_model)

        for model_fq in sorted(strict_models_set):
            if model_fq not in pair_model_set:
                strict_failures.append(f"STRICT {model_fq} has no pair")
                output_lines.append(f"STRICT {model_fq} has no pair")
            else:
                for pair in pairs:
                    on_model = model_fq in (pair.producer_model, pair.consumer_model)
                    if on_model and (pair.refused or pair.missing):
                        strict_failures.append(f"STRICT {model_fq} in {pair.topic}")
                        output_lines.append(
                            f"STRICT {model_fq} in {pair.topic}: "
                            f"refused={list(pair.refused)} missing={list(pair.missing)}"
                        )
                        break

        # Summary line
        pair_count = len(pairs)
        summary = (
            f"bus-model-seam: pairs={pair_count} explicit={explicit_pair_count} "
            f"inferred={inferred_pair_count} clean={clean_pair_count} "
            f"pinned={pinned_pair_count} unpinned={len(unpinned_pairs)} "
            f"stale={len(stale_pins)} strict_failures={len(strict_failures)}"
        )
        output_lines.append(summary)

        has_failures = bool(
            any(p.unresolved for p in pairs)
            or unpinned_pairs
            or stale_pins
            or strict_failures
        )

        return ModelBusModelSeamCheckResult(
            admitted=not has_failures,
            exit_code=int(has_failures),
            output_lines=tuple(output_lines),
            pair_count=pair_count,
            explicit_pair_count=explicit_pair_count,
            inferred_pair_count=inferred_pair_count,
            clean_pair_count=clean_pair_count,
            pinned_pair_count=pinned_pair_count,
            unpinned_pairs=tuple(unpinned_pairs),
            stale_pins=tuple(stale_pins),
            strict_failures=tuple(strict_failures),
            pairs=tuple(pairs),
        )
