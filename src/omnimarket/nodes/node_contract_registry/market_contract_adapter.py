# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Producer-side adapter: market node contract -> ModelHandlerContract payload.

Market node ``contract.yaml`` files declare ``handler`` / ``handler_routing``
and nest their I/O models; they do not carry the top-level ``handler_id`` /
``input_model`` / ``output_model`` / ``descriptor`` shape that the runtime
descriptor parser (``omnibase_infra`` ``ContractYamlParser.parse``) validates
against ``omnibase_core``'s ``ModelHandlerContract``.

This module transforms a parsed market contract mapping into a canonical
``ModelHandlerContract`` shell plus the typed runtime extensions that the
runtime parser validates independently. This lets the runtime hot-load market
nodes from their contract without editing the source contracts or pushing
market-specific derivation rules into ``omnibase_infra`` (OMN-12463, Approach A).

Derivation is deterministic and fail-fast: when a required field cannot be
derived from the contract, the adapter raises ``MarketContractAdapterError``
rather than emitting a malformed payload.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from omnibase_core.models.contracts.subcontracts.model_db_ownership_subcontract import (
    ModelDbOwnershipSubcontract,
)
from pydantic import ValidationError

# Canonical handler return type. Every ONEX handler's ``execute`` returns a
# ``ModelHandlerOutput``; it is the correct deterministic value for the
# ``output_model`` reference when a market contract declares no explicit output
# model. This is a real importable type, not a fabricated placeholder.
_DEFAULT_OUTPUT_MODEL = (
    "omnibase_core.models.dispatch.model_handler_output.ModelHandlerOutput"
)

# Market contracts use a wider set of archetype / purity vocabularies than the
# canonical ``EnumNodeArchetype`` (compute/effect/reducer/orchestrator) and
# ``ModelHandlerBehavior.purity`` (pure/side_effecting). These maps normalize
# the observed market vocabulary onto the canonical values the runtime parser
# requires (kafka_contract_source validates node_archetype against
# LiteralHandlerKind, and ModelHandlerBehavior pins purity to two literals).
_ARCHETYPE_NORMALIZATION: dict[str, str] = {
    "compute": "compute",
    "compute_generic": "compute",
    "effect": "effect",
    "effect_generic": "effect",
    "reducer": "reducer",
    "reducer_generic": "reducer",
    "orchestrator": "orchestrator",
    "orchestrator_generic": "orchestrator",
    # Non-archetype node_type values map onto their nearest archetype role.
    "service": "effect",
    "workflow": "orchestrator",
    "query": "compute",
}

_PURITY_NORMALIZATION: dict[str, str] = {
    "pure": "pure",
    "side_effecting": "side_effecting",
    "side_effect": "side_effecting",
    "effectful": "side_effecting",
    "impure": "side_effecting",
    "impure_when_recon": "side_effecting",
    "nondeterministic": "side_effecting",
    "orchestrating": "side_effecting",
}

# The dynamic runtime owns the typed interpretation of these fields. The
# registry producer carries only the canonical runtime-relevant event-bus
# extension surface; authoring metadata and the retired subscribe/publish
# mapping shapes stay in the source contract.
_CANONICAL_EVENT_BUS_FIELDS: tuple[str, ...] = (
    "subscribe_topics",
    "publish_topics",
    "dlq_topics",
    "consumer_group",
    "plugin_managed",
    "consumer_purpose",
    "tenant_scoped_ingress",
    "terminal_event",
)


class MarketContractAdapterError(ValueError):
    """Raised when a market contract cannot be mapped to ModelHandlerContract."""


def _require_str(value: Any, field: str, node_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MarketContractAdapterError(
            f"market contract '{node_name}' field '{field}' must be a non-empty "
            f"string, got {value!r}"
        )
    return value.strip()


def _normalize_model_ref(value: Any, field: str, node_name: str) -> str:
    """Normalize an I/O model declaration to a fully qualified reference string.

    Market contracts declare models either as a fully qualified string
    (``module.ClassName``), a bare class name, or a mapping with ``name`` and
    ``module`` keys. A mapping is joined into ``module.name``.
    """
    if isinstance(value, str):
        return _require_str(value, field, node_name)
    if isinstance(value, dict):
        name = value.get("name")
        module = value.get("module")
        name_str = _require_str(name, f"{field}.name", node_name)
        if isinstance(module, str) and module.strip():
            return f"{module.strip()}.{name_str}"
        # Bare class name with no module is still a valid opaque reference.
        return name_str
    raise MarketContractAdapterError(
        f"market contract '{node_name}' field '{field}' must be a string or "
        f"mapping, got {type(value).__name__}"
    )


def _first_routing_handler(contract: dict[str, Any]) -> dict[str, Any]:
    routing = contract.get("handler_routing")
    if isinstance(routing, dict):
        handlers = routing.get("handlers")
        if isinstance(handlers, list):
            for entry in handlers:
                if isinstance(entry, dict):
                    handler = entry.get("handler")
                    if isinstance(handler, dict):
                        return handler
    return {}


def _derive_handler_class(contract: dict[str, Any], node_name: str) -> str:
    """Derive the fully qualified ``module.ClassName`` handler path.

    Precedence: top-level ``handler.module`` + ``handler.class`` (the explicit
    declaration), then the first ``handler_routing`` handler's ``module`` +
    ``name``. The runtime imports this class to instantiate the handler, so a
    derivable class is mandatory.
    """
    top = contract.get("handler")
    if isinstance(top, dict) and top.get("module"):
        module = _require_str(top["module"], "handler.module", node_name)
        class_name = top.get("class") or top.get("name")
        class_str = _require_str(class_name, "handler.class", node_name)
        return f"{module}.{class_str}"

    routing_handler = _first_routing_handler(contract)
    if routing_handler.get("module"):
        module = _require_str(
            routing_handler["module"],
            "handler_routing.handlers[].handler.module",
            node_name,
        )
        class_name = routing_handler.get("name") or routing_handler.get("class")
        class_str = _require_str(
            class_name, "handler_routing.handlers[].handler.name", node_name
        )
        return f"{module}.{class_str}"

    raise MarketContractAdapterError(
        f"market contract '{node_name}' declares no handler module/class in "
        f"'handler' or 'handler_routing.handlers[].handler'; cannot derive "
        f"handler_class for hot-load"
    )


def _derive_input_model(contract: dict[str, Any], node_name: str) -> str:
    """Derive the input model reference from the contract's declared locations.

    Precedence mirrors the observed market contract shapes: top-level
    ``handler.input_model``, then per-routing-entry ``input_model`` /
    ``handler.input_model`` / ``event_model``, then a top-level ``input_model``.
    """
    top = contract.get("handler")
    if isinstance(top, dict) and top.get("input_model") is not None:
        return _normalize_model_ref(
            top["input_model"], "handler.input_model", node_name
        )

    routing = contract.get("handler_routing")
    if isinstance(routing, dict):
        handlers = routing.get("handlers")
        if isinstance(handlers, list):
            for entry in handlers:
                if not isinstance(entry, dict):
                    continue
                if entry.get("input_model") is not None:
                    return _normalize_model_ref(
                        entry["input_model"],
                        "handler_routing.handlers[].input_model",
                        node_name,
                    )
                handler = entry.get("handler")
                if isinstance(handler, dict) and handler.get("input_model") is not None:
                    return _normalize_model_ref(
                        handler["input_model"],
                        "handler_routing.handlers[].handler.input_model",
                        node_name,
                    )
                if isinstance(entry.get("event_model"), dict):
                    return _normalize_model_ref(
                        entry["event_model"],
                        "handler_routing.handlers[].event_model",
                        node_name,
                    )

    if contract.get("input_model") is not None:
        return _normalize_model_ref(contract["input_model"], "input_model", node_name)

    raise MarketContractAdapterError(
        f"market contract '{node_name}' declares no input model in 'handler', "
        f"'handler_routing.handlers[]', or top-level 'input_model'; cannot "
        f"derive input_model for hot-load"
    )


def _derive_output_model(contract: dict[str, Any], node_name: str) -> str:
    """Derive the output model reference, falling back to ModelHandlerOutput.

    Precedence: top-level ``handler.output_model``, then top-level
    ``output_model``. When neither is declared, fall back to the canonical
    handler return type ``ModelHandlerOutput`` — the type every handler's
    ``execute`` actually returns.
    """
    top = contract.get("handler")
    if isinstance(top, dict) and top.get("output_model") is not None:
        return _normalize_model_ref(
            top["output_model"], "handler.output_model", node_name
        )
    if contract.get("output_model") is not None:
        return _normalize_model_ref(contract["output_model"], "output_model", node_name)
    return _DEFAULT_OUTPUT_MODEL


def _derive_archetype(contract: dict[str, Any], node_name: str) -> str:
    """Derive the canonical node archetype from descriptor or node_type."""
    descriptor = contract.get("descriptor")
    raw: Any = None
    if isinstance(descriptor, dict):
        raw = descriptor.get("node_archetype")
    if raw is None:
        raw = contract.get("node_type")
    if not isinstance(raw, str) or not raw.strip():
        raise MarketContractAdapterError(
            f"market contract '{node_name}' declares no 'descriptor.node_archetype' "
            f"or 'node_type'; cannot derive node archetype"
        )
    normalized = _ARCHETYPE_NORMALIZATION.get(raw.strip().lower())
    if normalized is None:
        raise MarketContractAdapterError(
            f"market contract '{node_name}' has unmappable archetype/node_type "
            f"'{raw}'; expected one of {sorted(set(_ARCHETYPE_NORMALIZATION))}"
        )
    return normalized


def _derive_descriptor(contract: dict[str, Any], node_name: str) -> dict[str, Any]:
    """Build a ModelHandlerBehavior-shaped descriptor mapping.

    Carries idempotency and timeout when the market descriptor declares them;
    normalizes archetype and purity onto the canonical vocabularies.
    """
    archetype = _derive_archetype(contract, node_name)
    descriptor = contract.get("descriptor")
    descriptor = descriptor if isinstance(descriptor, dict) else {}

    raw_purity = descriptor.get("purity")
    if isinstance(raw_purity, str) and raw_purity.strip():
        purity = _PURITY_NORMALIZATION.get(raw_purity.strip().lower())
        if purity is None:
            raise MarketContractAdapterError(
                f"market contract '{node_name}' has unmappable purity "
                f"'{raw_purity}'; expected one of {sorted(set(_PURITY_NORMALIZATION))}"
            )
    else:
        # No declared purity: compute defaults to pure, others to side_effecting.
        purity = "pure" if archetype == "compute" else "side_effecting"

    behavior: dict[str, Any] = {
        "node_archetype": archetype,
        "purity": purity,
        "idempotent": bool(descriptor.get("idempotent", False)),
    }
    timeout = descriptor.get("timeout_ms")
    if isinstance(timeout, int):
        behavior["timeout_ms"] = timeout
    return behavior


def _runtime_extensions(contract: dict[str, Any], node_name: str) -> dict[str, Any]:
    """Validate and copy runtime extensions into the published payload.

    ``db_io`` is validated against the exact core ownership subcontract before
    publication. Routing remains in its source-declared shape because the
    runtime's materialization adapter owns conversion to its wiring model.
    Event-bus authoring metadata is intentionally not placed on the runtime
    registration wire; only fields consumed by runtime wiring are retained.
    """
    extensions: dict[str, Any] = {}

    db_io = contract.get("db_io")
    if db_io is not None:
        try:
            ModelDbOwnershipSubcontract.model_validate(db_io)
        except ValidationError as exc:
            raise MarketContractAdapterError(
                f"market contract '{node_name}' field 'db_io' is not the exact "
                f"typed database ownership subcontract: {exc}"
            ) from exc
        extensions["db_io"] = deepcopy(db_io)

    handler_routing = contract.get("handler_routing")
    if handler_routing is not None:
        if not isinstance(handler_routing, dict):
            raise MarketContractAdapterError(
                f"market contract '{node_name}' field 'handler_routing' must be "
                f"a mapping, got {type(handler_routing).__name__}"
            )
        extensions["handler_routing"] = deepcopy(handler_routing)

    event_bus = contract.get("event_bus")
    root_terminal_event = contract.get("terminal_event")
    if event_bus is not None or root_terminal_event is not None:
        if event_bus is None:
            event_bus = {}
        if not isinstance(event_bus, dict):
            raise MarketContractAdapterError(
                f"market contract '{node_name}' field 'event_bus' must be a "
                f"mapping, got {type(event_bus).__name__}"
            )
        canonical_event_bus = {
            field: deepcopy(event_bus[field])
            for field in _CANONICAL_EVENT_BUS_FIELDS
            if field in event_bus
        }
        if (
            "terminal_event" not in canonical_event_bus
            and root_terminal_event is not None
        ):
            canonical_event_bus["terminal_event"] = _require_str(
                root_terminal_event, "terminal_event", node_name
            )
        if canonical_event_bus:
            extensions["event_bus"] = canonical_event_bus

    return extensions


def to_handler_contract_payload(
    contract: dict[str, Any], node_name: str
) -> dict[str, Any]:
    """Transform a parsed market contract into a runtime registration payload.

    The returned mapping carries a canonical ``ModelHandlerContract`` shell,
    the derived ``handler_class`` under ``metadata.handler_class`` (the location
    the runtime descriptor parser reads), and independently typed ``db_io``,
    ``handler_routing``, and ``event_bus`` runtime extensions.

    Args:
        contract: Parsed market node ``contract.yaml`` mapping.
        node_name: Node identifier, used for handler_id derivation and errors.

    Returns:
        A runtime registration payload dict.

    Raises:
        MarketContractAdapterError: If a required field cannot be derived.
    """
    if not isinstance(contract, dict):
        raise MarketContractAdapterError(
            f"market contract '{node_name}' must be a mapping, got "
            f"{type(contract).__name__}"
        )

    name = _require_str(contract.get("name"), "name", node_name)
    contract_version = contract.get("contract_version")
    if not isinstance(contract_version, dict):
        raise MarketContractAdapterError(
            f"market contract '{node_name}' field 'contract_version' must be a "
            f"{{major, minor, patch}} mapping, got {contract_version!r}"
        )

    handler_class = _derive_handler_class(contract, node_name)
    descriptor = _derive_descriptor(contract, node_name)

    # handler_id uses the 'node.' prefix so the ModelHandlerContract archetype
    # consistency validator (which forces archetype-specific prefixes to match)
    # accepts any archetype. The node name segment is sanitized to satisfy the
    # validator's per-segment "starts with letter or underscore" rule.
    name_segment = name.strip().replace("-", "_").replace(" ", "_")
    handler_id = f"node.{name_segment}"

    # Preserve any existing metadata; inject handler_class where the runtime
    # descriptor parser reads it without clobbering other metadata keys.
    existing_metadata = contract.get("metadata")
    metadata: dict[str, Any] = (
        dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
    )
    metadata["handler_class"] = handler_class

    payload: dict[str, Any] = {
        "handler_id": handler_id,
        "name": name,
        "contract_version": contract_version,
        "descriptor": descriptor,
        "input_model": _derive_input_model(contract, node_name),
        "output_model": _derive_output_model(contract, node_name),
        "handler_class": handler_class,
        "metadata": metadata,
        **_runtime_extensions(contract, node_name),
    }

    description = contract.get("description")
    if isinstance(description, str) and description.strip():
        # ModelHandlerContract caps description at 1000 chars.
        payload["description"] = description.strip()[:1000]

    return payload


__all__ = [
    "MarketContractAdapterError",
    "to_handler_contract_payload",
]
