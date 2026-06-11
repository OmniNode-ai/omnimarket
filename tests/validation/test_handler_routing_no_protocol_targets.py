# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recurrence guard: no contract handler_routing target may be a typing.Protocol.

Regression coverage for OMN-12956. A ``handler_routing`` entry that resolves to a
``typing.Protocol`` subclass (e.g. the Phase-0 ``EvidenceEvaluator`` /
``SideEffectObserver`` stubs) is non-instantiable. When such a contract defaults
to the ``main`` runtime profile, ``omnibase_core.ServiceHandlerResolver.resolve``
reaches its zero-arg instantiation path and raises
``TypeError: Protocols cannot be instantiated``, crash-looping main-runtime
bootstrap on any infra build predating the OMN-12501 quarantine guard.

This test loads every omnimarket contract's ``handler_routing`` targets, imports
each referenced class, and asserts none is a ``typing.Protocol`` (or otherwise
non-instantiable). It must fail on the pre-fix ``node_overseer_observer`` contract.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"


def _load_contract(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict)
    return raw


def _routing_targets(raw: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Return (module, class_name) for every handler_routing target in a contract."""
    routing_raw = raw.get("handler_routing")
    if not isinstance(routing_raw, dict):
        return ()

    targets: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(module: object, class_name: object) -> None:
        if not isinstance(module, str) or not isinstance(class_name, str):
            return
        if not module or not class_name:
            return
        ref = (module, class_name)
        if ref not in seen:
            seen.add(ref)
            targets.append(ref)

    for entry in routing_raw.get("handlers", []):
        if not isinstance(entry, dict):
            continue
        # Flat form: handler_module + handler_class
        add(entry.get("handler_module"), entry.get("handler_class"))
        # Nested form: handler: {module, name|class}
        nested = entry.get("handler")
        if isinstance(nested, dict):
            add(nested.get("module"), nested.get("name") or nested.get("class"))

    return tuple(targets)


def _routing_target_cases() -> tuple[Any, ...]:
    cases: list[Any] = []
    for contract_path in sorted(NODES_ROOT.glob("*/contract.yaml")):
        raw = _load_contract(contract_path)
        for module, class_name in _routing_targets(raw):
            cases.append(
                pytest.param(
                    contract_path,
                    module,
                    class_name,
                    id=f"{contract_path.parent.name}:{class_name}",
                )
            )
    return tuple(cases)


def _is_protocol_class(cls: type) -> bool:
    """Mirror ``omnibase_core`` resolver: a class is a Protocol iff it has the
    truthy ``_is_protocol`` sentinel set by ``typing.Protocol``."""
    return bool(getattr(cls, "_is_protocol", False))


_ROUTING_TARGET_CASES = _routing_target_cases()


def test_at_least_one_routing_target_exists() -> None:
    """Sanity: the scan finds handler_routing targets to validate."""
    assert _ROUTING_TARGET_CASES, (
        "no handler_routing targets discovered under "
        f"{NODES_ROOT}; the recurrence guard would be vacuous"
    )


@pytest.mark.parametrize(
    ("contract_path", "module", "class_name"),
    _ROUTING_TARGET_CASES,
)
def test_handler_routing_target_is_not_protocol(
    contract_path: Path,
    module: str,
    class_name: str,
) -> None:
    """No handler_routing target may resolve to a ``typing.Protocol`` class.

    This mirrors the runtime resolution path
    (``omnibase_infra ... _import_handler_class`` →
    ``getattr(import_module(module), name)``). When the target resolves to a
    class, that class is what ``omnibase_core.ServiceHandlerResolver`` would
    instantiate; a ``typing.Protocol`` there raises
    ``TypeError: Protocols cannot be instantiated`` at the resolver's zero-arg
    path and crash-loops main-runtime bootstrap (OMN-12956).

    Scope note: this guard validates the Protocol-instantiation failure mode
    specifically. Some omnimarket contracts use ``handler.name`` as a logical
    routing key that is not a top-level class symbol in its module (e.g.
    sub-handler probe modules); those cannot reach the Protocol-instantiation
    path and are explicitly out of scope here (covered by the runtime-ownership
    and compliance sweeps). Such entries are skipped, not failed, so this guard
    stays a precise regression lock for the resolver crash rather than a broad
    contract-hygiene check.
    """
    imported = importlib.import_module(module)
    target = getattr(imported, class_name, None)
    if not isinstance(target, type):
        pytest.skip(
            f"{contract_path.parent.name}: routing target {module}.{class_name} "
            "does not resolve to a top-level class; out of scope for the "
            "Protocol-instantiation guard (OMN-12956)"
        )
    assert not _is_protocol_class(target), (
        f"{contract_path.parent.name}: handler_routing target "
        f"{module}.{class_name} is a typing.Protocol and is non-instantiable; "
        "route to the concrete Null/real implementation instead (OMN-12956)"
    )
