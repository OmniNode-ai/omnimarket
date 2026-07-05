#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill-mapping input-coverage gate (OMN-13965).

Enforces that every ``skill_mapping.yaml`` argument carrying a ``default`` is an
accepted field on the input model its backing node actually validates at runtime.

Why this exists (OMN-13964)
---------------------------
``onex skill <name>`` resolves the declarative ``skill_mapping.yaml`` (shipped in
the installed ``omnibase_infra`` package) and, for every arg that declares a
``default``, injects ``payload_field: <default>`` into the node-input payload —
on *every* invocation, even a minimal one. ``RuntimeLocal`` then validates that
payload against the model the handler actually consumes. For ``create_ticket``
that model is ``ModelCreateTicketRequest`` (``extra="forbid"``); it was missing
the injected ``allow_arch_violation`` field, so the skill failed 100% of CLI
invocations with ``extra_forbidden`` before doing any work. Any defaulted
skill_mapping arg whose ``payload_field`` is not accepted by the node's input
model silently bricks that skill's CLI path. This gate makes that drift a
hard CI failure instead of a live dogfood-rail wound.

What "the input model" means here
---------------------------------
The runtime validates against the model the *handler* consumes, NOT the
contract's declared ``handler.input_model`` (that field points at
``ModelCreateTicketStartCommand`` for create_ticket — the very field that had
drifted). Empirically (OMN-13964, verified 2026-07-05) the validated model is:

  * COMPUTE/EFFECT/REDUCER handlers: the first non-``self`` parameter annotation
    of ``handle()`` (e.g. ``handle(request: ModelCreateTicketRequest)``).
  * ORCHESTRATOR handlers: the ``event_model`` declared in
    ``handler_routing.handlers[]`` (``handle()`` takes a ``ModelEventEnvelope``,
    not the domain payload).

So the *candidate set* of payload models for a skill is the union of:
  - handle() first-param BaseModel members, excluding envelope wrappers
  - handler_routing.handlers[].event_model BaseModel classes

A defaulted arg is a VIOLATION iff **no** candidate accepts it (not a declared
field AND the candidate is ``extra="forbid"``). "Accepted by at least one
candidate" is the correct invariant because the runtime builds whichever
candidate accepts the payload -- proven by ``plan_to_tickets`` (a ``handle()``
``Union`` whose ``StartCommand`` member accepts the args the ``Request`` member
does not) validating cleanly at runtime.

Exit codes:
  0 — every defaulted arg is accepted by its node's input model (skips allowed)
  1 — one or more defaulted-arg / missing-field violations

Flags:
  --json    Machine-readable JSON report on stdout.
  --strict  Also fail when a skill's input model cannot be resolved at all
            (default: unresolved skills are reported as SKIP, not FAIL).
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys
import types
import typing

from pydantic import BaseModel


def _basemodel_members(annotation: object) -> list[type[BaseModel]]:
    """Return the concrete ``BaseModel`` subclasses reachable from ``annotation``.

    Unwraps ``Union``/``Optional`` and generic aliases (e.g.
    ``ModelEventEnvelope[T]`` → ``ModelEventEnvelope``). Non-model members
    (``dict``, ``None``, primitives) are dropped.
    """
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        members: list[type[BaseModel]] = []
        for arg in typing.get_args(annotation):
            members.extend(_basemodel_members(arg))
        return members
    # Generic alias such as ModelEventEnvelope[T]: the origin is the model class.
    if isinstance(origin, type) and issubclass(origin, BaseModel):
        return [origin]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return []


def _is_envelope(model: type[BaseModel]) -> bool:
    """Envelope wrappers carry the payload, not the domain fields — exclude them."""
    return "Envelope" in model.__name__


def _import_symbol(module: str, name: str) -> object | None:
    try:
        symbol: object = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        return None
    return symbol


def _handle_param_models(handler_cls: type) -> list[type[BaseModel]]:
    """BaseModel members of ``handle()``'s first non-self parameter (no envelopes)."""
    handle = getattr(handler_cls, "handle", None)
    if handle is None:
        return []
    try:
        sig = inspect.signature(handle)
    except (TypeError, ValueError):
        return []
    params = [p for p in sig.parameters.values() if p.name != "self"]
    if not params:
        return []
    try:
        hints = typing.get_type_hints(handle)
    except Exception:
        hints = {}
    annotation = hints.get(params[0].name, params[0].annotation)
    return [m for m in _basemodel_members(annotation) if not _is_envelope(m)]


def _resolve_candidate_models(contract: dict[str, object]) -> list[type[BaseModel]]:
    """Collect every payload model the runtime could validate a skill payload against."""
    candidates: list[type[BaseModel]] = []

    handler = contract.get("handler")
    routing = contract.get("handler_routing")
    routing_handlers: list[object] = []
    if isinstance(routing, dict) and isinstance(routing.get("handlers"), list):
        routing_handlers = list(routing["handlers"])

    # Handler class: top-level handler block, else handler_routing default/first.
    module: str | None = None
    class_name: str | None = None
    if isinstance(handler, dict):
        module = handler.get("module")
        class_name = handler.get("class")
    if not (module and class_name) and isinstance(routing, dict):
        default_handler = routing.get("default_handler")
        if isinstance(default_handler, dict):
            module = default_handler.get("module")
            class_name = default_handler.get("class") or default_handler.get("name")
    if not (module and class_name) and routing_handlers:
        first = routing_handlers[0]
        if isinstance(first, dict) and isinstance(first.get("handler"), dict):
            hh = first["handler"]
            module = hh.get("module")
            # handler_routing entries use `name`, not `class`, for the class.
            class_name = hh.get("class") or hh.get("name")

    if module and class_name:
        handler_cls = _import_symbol(module, class_name)
        if isinstance(handler_cls, type):
            candidates.extend(_handle_param_models(handler_cls))

    # Orchestrator payload models: handler_routing[].event_model.
    for entry in routing_handlers:
        if not isinstance(entry, dict):
            continue
        event_model = entry.get("event_model")
        if isinstance(event_model, dict):
            em = _import_symbol(
                str(event_model.get("module", "")), str(event_model.get("name", ""))
            )
            if isinstance(em, type) and issubclass(em, BaseModel):
                candidates.append(em)

    # Dedup, preserving order.
    seen: set[type[BaseModel]] = set()
    unique: list[type[BaseModel]] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _accepts(model: type[BaseModel], field: str) -> bool:
    """A model accepts ``field`` if it declares it or permits extra inputs."""
    if field in model.model_fields:
        return True
    return model.model_config.get("extra") != "forbid"


def evaluate() -> dict[str, object]:
    """Evaluate every skill; return a structured report."""
    # Imported lazily so `--help` works without the omnibase_infra install.
    import yaml
    from omnibase_infra.cli.cli_skill import (
        _resolve_packaged_contract,
        load_skill_registry,
    )

    registry = load_skill_registry()
    results: list[dict[str, object]] = []
    for skill in sorted(registry.skills, key=lambda s: s.skill_name):
        defaulted = [
            arg.payload_field
            for arg in skill.args
            if getattr(arg, "default", None) is not None
        ]
        entry: dict[str, object] = {
            "skill": skill.skill_name,
            "node": skill.node_name,
            "defaulted_args": defaulted,
        }
        if not defaulted:
            entry["status"] = "PASS"
            entry["detail"] = "no defaulted args"
            results.append(entry)
            continue
        try:
            contract = yaml.safe_load(
                _resolve_packaged_contract(skill.node_name).read_text()
            )
        except Exception as exc:
            entry["status"] = "SKIP"
            entry["detail"] = f"contract load failed: {exc}"
            results.append(entry)
            continue
        candidates = _resolve_candidate_models(
            contract if isinstance(contract, dict) else {}
        )
        if not candidates:
            entry["status"] = "SKIP"
            entry["detail"] = "could not resolve any input model for this node"
            results.append(entry)
            continue
        entry["candidate_models"] = [f"{c.__module__}.{c.__name__}" for c in candidates]
        violations = [
            field
            for field in defaulted
            if not any(_accepts(c, field) for c in candidates)
        ]
        if violations:
            entry["status"] = "FAIL"
            entry["missing_fields"] = violations
        else:
            entry["status"] = "PASS"
        results.append(entry)

    fails = [r for r in results if r["status"] == "FAIL"]
    skips = [r for r in results if r["status"] == "SKIP"]
    return {
        "checked": len(results),
        "pass": sum(1 for r in results if r["status"] == "PASS"),
        "fail": len(fails),
        "skip": len(skips),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail when a skill's input model cannot be resolved (SKIP → FAIL).",
    )
    args = parser.parse_args(argv)

    report = evaluate()
    results = typing.cast("list[dict[str, object]]", report["results"])

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for r in results:
            marker = {"PASS": "ok  ", "FAIL": "FAIL", "SKIP": "skip"}[str(r["status"])]
            line = f"  {marker} {r['skill']:34s} ({r['node']})"
            if r["status"] == "FAIL":
                line += f" -> defaulted args not accepted by any input model: {r['missing_fields']}"
            elif r["status"] == "SKIP":
                line += f" -> {r['detail']}"
            print(line)
        print(
            f"\nskill-mapping-input-coverage: "
            f"PASS {report['pass']}  FAIL {report['fail']}  SKIP {report['skip']}"
        )

    fail_count = typing.cast("int", report["fail"])
    skip_count = typing.cast("int", report["skip"])
    strict_skip_fail = args.strict and skip_count > 0
    if fail_count > 0 or strict_skip_fail:
        if not args.json:
            print("\nskill-mapping-input-coverage: FAIL", file=sys.stderr)
        return 1
    if not args.json:
        print("skill-mapping-input-coverage: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
