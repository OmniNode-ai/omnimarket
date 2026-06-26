# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract guard (OMN-13551): every handler_routing target must be boot-resolvable.

At dev-HEAD boot the runtime walks each contract's ``handler_routing.handlers``
and asks ``ServiceHandlerResolver`` to instantiate the declared handler. The
only providers available at that point are the three known-injectable params
(``event_bus``, ``container``, ``ownership_query``). A handler whose constructor
(or function signature, for function-form handlers) requires any *other*
parameter with no default cannot be resolved -> the resolver raises ``TypeError``
and the runtime quarantines the handler with a boot warning.

OMN-13551 surfaced ~17 such quarantines in the 2026-06-24 redeploy. The bulk
were *helper* functions/classes (probers, adapters, record writers, pure compute
functions) that were wrongly listed as ``handler_routing`` entries even though
they are called internally by the node's real handler — not dispatched by the
runtime. The fix removes those misclassified entries (the node keeps its real
handler) and repoints node_intelligence_orchestrator at its envelope-shaped
wrapper classes.

A small documented allowlist remains for handlers that take a genuine,
protocol-typed external dependency (project tracker, generated-tool registry,
task dispatcher) which is not yet registered in the boot container. Wiring those
live adapters into runtime boot is tracked separately (OMN-13603); until then they are the
*only* permitted boot-quarantines and are explicitly enumerated so a *new*
misclassified entry fails this test.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

# Providers the runtime can inject at boot via known-param injection
# (ServiceHandlerResolver step 4). Any other required, default-less param is
# unresolvable and quarantines the handler.
_KNOWN_INJECTABLE = frozenset({"event_bus", "container", "ownership_query"})

_CONCRETE_PARAM_KINDS = (
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY,
)

# Documented carve-out (OMN-13551): real, correctly-shaped handlers whose
# protocol-typed external dependency is not yet registered in the boot
# container. These are the ONLY permitted boot-quarantines. Format:
# "module.attr" -> reason. Wiring the backing adapter into boot is follow-up
# provisioning work, not a handler-prep defect.
_UNWIRED_DEPENDENCY_ALLOWLIST: dict[str, str] = {
    "omnimarket.nodes.node_ticket_query.handlers.handler_ticket_query.HandlerTicketQuery": (
        "needs ProtocolProjectTracker adapter registered in boot container"
    ),
    "omnimarket.nodes.node_tool_reuse_matcher_compute.handlers."
    "handler_tool_reuse_matcher.HandlerToolReuseMatcher": (
        "needs ProtocolGeneratedToolRegistry adapter registered in boot container"
    ),
    "omnimarket.nodes.node_skill_overseer_verify_orchestrator.handlers."
    "handler_skill_requested.handle_skill_requested": (
        "needs an injected TaskDispatcher provider in boot container"
    ),
}

_NODES_DIR = Path(__file__).parent.parent / "src" / "omnimarket" / "nodes"


def _required_non_injectable_params(obj: Callable[..., Any]) -> list[str]:
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return []
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if (
            param.kind in _CONCRETE_PARAM_KINDS
            and param.default is inspect.Parameter.empty
            and name not in _KNOWN_INJECTABLE
        ):
            required.append(name)
    return required


def _iter_declared_handlers() -> list[tuple[str, str, str, Callable[..., Any]]]:
    """Yield (node, operation, fqn, handler_obj) for every handler_routing entry."""
    out: list[tuple[str, str, str, Callable[..., Any]]] = []
    for contract in sorted(_NODES_DIR.glob("*/contract.yaml")):
        node = contract.parent.name
        data = yaml.safe_load(contract.read_text())
        routing = (data or {}).get("handler_routing") or {}
        for entry in routing.get("handlers") or []:
            handler = entry.get("handler") or {}
            module = handler.get("module")
            name = handler.get("name")
            if not module or not name:
                continue
            operation = entry.get("operation", "?")
            mod = importlib.import_module(module)
            obj = getattr(mod, name)
            out.append((node, operation, f"{module}.{name}", obj))
    return out


def test_all_handler_routing_targets_are_boot_resolvable() -> None:
    """Every handler_routing entry resolves at boot, except the documented carve-out."""
    quarantines: list[str] = []
    for node, operation, fqn, obj in _iter_declared_handlers():
        unresolvable = _required_non_injectable_params(obj)
        if not unresolvable:
            continue
        if fqn in _UNWIRED_DEPENDENCY_ALLOWLIST:
            continue
        quarantines.append(
            f"  {node} :: op={operation} :: {fqn} :: "
            f"required-unresolvable={unresolvable}"
        )

    assert not quarantines, (
        f"{len(quarantines)} handler_routing entr(ies) would quarantine at boot "
        "(non-injectable required ctor params). These are not runtime-dispatched "
        "handlers — remove the misclassified entry (the node keeps its real "
        "handler) or repoint at an envelope-shaped wrapper:\n" + "\n".join(quarantines)
    )


def test_allowlist_entries_still_exist_and_still_need_deps() -> None:
    """Guard the carve-out: every allowlisted handler must still be declared and
    still actually require an unwired dependency. A stale allowlist entry (handler
    removed, or dependency now wired) must be deleted from the allowlist."""
    declared = {fqn: obj for _, _, fqn, obj in _iter_declared_handlers()}
    for fqn in _UNWIRED_DEPENDENCY_ALLOWLIST:
        assert fqn in declared, (
            f"Allowlisted handler {fqn!r} is no longer declared in any "
            "handler_routing — remove it from _UNWIRED_DEPENDENCY_ALLOWLIST."
        )
        assert _required_non_injectable_params(declared[fqn]), (
            f"Allowlisted handler {fqn!r} no longer requires an unwired "
            "dependency — its DI is satisfied, so remove it from "
            "_UNWIRED_DEPENDENCY_ALLOWLIST."
        )
