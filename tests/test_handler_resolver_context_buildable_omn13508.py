# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression guard (OMN-13508): every contract handler ref must build a valid
runtime resolver context.

## Why this guard exists

Dev runtime 0.38.4 booted with 9 non-strict auto-wiring failures. Two distinct
defect classes were responsible:

1. **Symbol drift (8 of 9)** -- a ``handler_routing`` entry named a module-level
   symbol that no longer existed (``AttributeError`` /
   ``CLASS_NOT_FOUND (HANDLER_LOADER_011)``). Fixed by OMN-13550 / OMN-13551.

2. **Non-type handler symbol (1 of 9)** -- ``node_intelligence_orchestrator``
   pointed ``handler_routing`` at a bare ``handle_receive_intent(intent)``
   *function*. The symbol *existed* (so the OMN-13550 ``hasattr`` guard passed),
   but the runtime then built
   ``ModelHandlerResolverContext(handler_cls=<function>)`` and Pydantic rejected
   it with ``ValidationError: Input should be a type`` because ``handler_cls``
   is typed ``type``. Fixed by OMN-13551 (repoint at the envelope-shaped wrapper
   *class*).

The pre-existing guards each cover only part of this:

* ``test_handler_class_existence_omn13550.py`` checks ``hasattr(module, name)``
  for ``handler_routing`` entries -- catches class (1) but a *function* symbol
  exists, so it sails past class (2).
* ``test_handler_routing_boot_resolvable.py`` checks constructor params for
  ``handler_routing`` entries -- catches the unsatisfiable-ctor quarantine
  class, not the ``handler_cls`` *type* validation.
* Neither scans the **top-level** ``handler:`` block -- only ``handler_routing``.

This guard closes both gaps by replicating exactly what
``omnibase_infra.runtime.auto_wiring.handler_wiring._prepare_handler_wiring``
does at boot: import the symbol and construct ``ModelHandlerResolverContext``.
A contract whose handler ref cannot do both would fail auto-wiring at runtime
startup -- that is a build defect, caught here pre-merge instead of on the
deployed runtime (Operating Rule 5: enforcement, not detection).

## Allowlist

A handler ref may legitimately resolve to a non-class callable (a function-form
handler whose backing provider is not yet wired into the boot container). Those
are enumerated in
``test_handler_routing_boot_resolvable._UNWIRED_DEPENDENCY_ALLOWLIST``; this
guard reuses that single source of truth so a new drift cannot hide behind a
stale carve-out.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.resolver.model_handler_resolver_context import (
    ModelHandlerResolverContext,
)
from pydantic import ValidationError

# Reuse the single allowlist that the boot-resolvability guard already owns so
# the two guards cannot drift apart.
from tests.test_handler_routing_boot_resolvable import (
    _UNWIRED_DEPENDENCY_ALLOWLIST,
)

_NODES_DIR = Path(__file__).resolve().parent.parent / "src" / "omnimarket" / "nodes"


def _handler_refs() -> list[tuple[str, str, str, str]]:
    """Yield (contract_rel, location, handler_module, handler_name) for every
    contract handler ref -- the top-level ``handler:`` block *and* each
    ``handler_routing.handlers[].handler`` entry.

    The top-level block keys the class under ``class``; routing entries key it
    under ``name`` (verified across all omnimarket contracts).
    """
    refs: list[tuple[str, str, str, str]] = []
    for contract_path in sorted(_NODES_DIR.glob("*/contract.yaml")):
        data = yaml.safe_load(contract_path.read_text())
        if not isinstance(data, dict):
            continue
        rel = contract_path.parent.name

        top = data.get("handler")
        if isinstance(top, dict):
            module = top.get("module")
            name = top.get("class") or top.get("name")
            if isinstance(module, str) and isinstance(name, str):
                refs.append((rel, "handler", module, name))

        routing = data.get("handler_routing") or {}
        if isinstance(routing, dict):
            for i, entry in enumerate(routing.get("handlers") or []):
                if not isinstance(entry, dict):
                    continue
                handler = entry.get("handler") or {}
                module = handler.get("module")
                name = handler.get("name") or handler.get("class")
                if isinstance(module, str) and isinstance(name, str):
                    refs.append((rel, f"handler_routing[{i}].handler", module, name))
    return refs


_REFS = _handler_refs()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("contract_rel", "location", "handler_module", "handler_name"),
    _REFS,
    ids=[f"{rel}::{loc}::{name}" for rel, loc, _, name in _REFS],
)
def test_handler_ref_builds_resolver_context(
    contract_rel: str, location: str, handler_module: str, handler_name: str
) -> None:
    """Importing the handler symbol and building ModelHandlerResolverContext
    must both succeed -- exactly the runtime auto-wiring boot path.

    Catches OMN-13508 defect class (1) symbol drift (import / AttributeError)
    and class (2) non-type handler symbol (resolver-context ValidationError
    "Input should be a type").
    """
    fqn = f"{handler_module}.{handler_name}"

    module = importlib.import_module(handler_module)
    assert hasattr(module, handler_name), (
        f"CLASS_NOT_FOUND: '{handler_name}' does not exist in module "
        f"'{handler_module}', declared in {contract_rel}::{location}. "
        f"Available: {sorted(n for n in dir(module) if not n.startswith('_'))}"
    )
    handler_cls = getattr(module, handler_name)

    try:
        ModelHandlerResolverContext(
            handler_cls=handler_cls,
            handler_module=handler_module,
            handler_name=handler_name,
            contract_name=contract_rel,
            node_name=contract_rel,
        )
    except ValidationError as exc:
        if fqn in _UNWIRED_DEPENDENCY_ALLOWLIST:
            pytest.skip(
                f"{fqn} is a documented function-form / unwired-dependency "
                f"carve-out: {_UNWIRED_DEPENDENCY_ALLOWLIST[fqn]}"
            )
        pytest.fail(
            f"HANDLER_CLS_NOT_A_TYPE: '{fqn}' (declared in "
            f"{contract_rel}::{location}) resolved to "
            f"{type(handler_cls).__name__}, not a class. The runtime builds "
            f"ModelHandlerResolverContext(handler_cls=...) at boot and Pydantic "
            f"requires a type -- this contract would fail auto-wiring at runtime "
            f"startup (OMN-13508 defect class 2). Repoint the entry at an "
            f"envelope-shaped wrapper *class*, not a bare function.\n{exc}"
        )


@pytest.mark.unit
def test_at_least_one_handler_ref_scanned() -> None:
    """Guard against the parametrize list silently collapsing to empty."""
    assert _REFS, "no handler refs discovered -- scan is broken"


@pytest.mark.unit
def test_guard_fires_on_non_type_symbol() -> None:
    """Self-test: prove the resolver-context guard actually rejects a
    function-form handler symbol (the OMN-13508 #9 defect class).

    Uses a real function symbol so a future refactor that makes
    ModelHandlerResolverContext accept non-types would fail this test loudly
    rather than silently weakening the guard."""

    def _not_a_class() -> None:  # a callable that is not a type
        return None

    assert not isinstance(_not_a_class, type)
    with pytest.raises(ValidationError):
        ModelHandlerResolverContext(
            handler_cls=_not_a_class,
            handler_module="m",
            handler_name="n",
            contract_name="c",
            node_name="x",
        )
