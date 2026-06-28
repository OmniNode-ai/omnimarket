# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler ``handle()`` signature conformance with the RuntimeLocal adapter.

Systemic Finding #4 (OMN-13711): ``RuntimeLocal``'s ``LocalRuntimeBusAdapter``
dispatches a contract-declared entry handler by introspecting its ``handle()``
signature (``omnibase_core.runtime.runtime_local_adapter._invoke_handle_method``).
When the single positional parameter is a concrete ``BaseModel`` (a typed
command) the adapter passes the resolved model instance positionally. But when
``handle()`` declares **additional positional parameters** (e.g. an optional
``phase_results`` second positional), the adapter falls through to its
``handle_method(**payload_dict)`` branch and explodes with::

    TypeError: Handler<X>.handle() got an unexpected keyword argument 'correlation_id'

because ``correlation_id`` is a field of the command payload, not a parameter of
``handle()``. This crashed two user-facing mapped skills (``build_loop`` →
``HandlerBuildLoop`` and ``design_to_plan`` → ``HandlerDesignToPlan``).

The fix makes every secondary ``handle()`` parameter keyword-only (``*,``) so the
adapter sees a single positional concrete-model parameter and dispatches it
positionally. These tests:

1. Reproduce the exact failure path through the real adapter for the two named
   handlers and assert it no longer raises the ``correlation_id`` ``TypeError``.
2. Enforce a repo-wide invariant so this cannot regress: every contract-declared
   entry handler whose ``handle()`` first parameter is a concrete ``BaseModel``
   must expose exactly one positional parameter (extras must be keyword-only).
"""

from __future__ import annotations

import importlib
import inspect
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.runtime.runtime_local_adapter import _invoke_handle_method
from pydantic import BaseModel

_NODES_ROOT = Path(__file__).resolve().parent.parent / "src" / "omnimarket" / "nodes"


def _positional_params(handle: object) -> list[inspect.Parameter]:
    """Return the POSITIONAL_OR_KEYWORD / POSITIONAL_ONLY params after ``self``."""
    try:
        signature = inspect.signature(handle, eval_str=True)
    except (TypeError, ValueError, NameError):
        signature = inspect.signature(handle)
    return [
        param
        for name, param in signature.parameters.items()
        if name != "self"
        and param.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]


def _first_param_is_concrete_model(handle: object) -> bool:
    positional = _positional_params(handle)
    if not positional:
        return False
    annotation = positional[0].annotation
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _iter_contract_entry_handlers() -> list[tuple[str, str, object]]:
    """Yield ``(node_name, class_name, handle_method)`` for every contract handler."""
    discovered: list[tuple[str, str, object]] = []
    for contract in sorted(_NODES_ROOT.glob("*/contract.yaml")):
        try:
            data = yaml.safe_load(contract.read_text())
        except yaml.YAMLError:
            continue
        handler = (data or {}).get("handler") or {}
        module_name = handler.get("module")
        class_name = handler.get("class")
        if not module_name or not class_name:
            continue
        try:
            klass = getattr(importlib.import_module(module_name), class_name)
        except (ImportError, AttributeError):
            continue
        handle = getattr(klass, "handle", None)
        if handle is None:
            continue
        discovered.append((contract.parent.name, class_name, handle))
    return discovered


_TYPED_COMMAND_HANDLERS = [
    pytest.param(node, cls, handle, id=node)
    for node, cls, handle in _iter_contract_entry_handlers()
    if _first_param_is_concrete_model(handle)
]


@pytest.mark.unit
@pytest.mark.parametrize(("node", "cls", "handle"), _TYPED_COMMAND_HANDLERS)
def test_typed_command_handle_has_single_positional_param(
    node: str, cls: str, handle: object
) -> None:
    """Regression guard for OMN-13711.

    A contract entry handler that receives a typed command (concrete ``BaseModel``
    first positional parameter) must declare **exactly one** positional parameter.
    Any additional configuration parameter must be keyword-only (``*,``); otherwise
    the RuntimeLocal adapter falls into its ``handle_method(**payload_dict)`` branch
    and rejects the command's own fields (``correlation_id``, ...) as unexpected
    kwargs.
    """
    positional = _positional_params(handle)
    extra = [p.name for p in positional[1:]]
    assert len(positional) == 1, (
        f"{cls}.handle() ({node}) declares extra positional parameter(s) {extra!r} "
        f"after its typed command; make them keyword-only (insert '*,') so the "
        f"RuntimeLocal adapter dispatches the command positionally instead of via "
        f"**payload_dict (OMN-13711)."
    )


@pytest.mark.unit
def test_build_loop_handle_accepts_runtime_dispatch() -> None:
    """Reproduce OMN-13711 through the real adapter for HandlerBuildLoop.

    Before the fix this raised
    ``TypeError: HandlerBuildLoop.handle() got an unexpected keyword argument
    'correlation_id'``. After making ``phase_results`` keyword-only the adapter
    passes the command model positionally and ``handle()`` runs.
    """
    from omnimarket.nodes.node_build_loop.handlers.handler_build_loop import (
        HandlerBuildLoop,
    )
    from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
        ModelLoopStartCommand,
    )

    command = ModelLoopStartCommand(
        correlation_id=uuid.uuid4(),
        requested_at=datetime.now(tz=UTC),
        dry_run=True,
    )
    result = _invoke_handle_method(HandlerBuildLoop().handle, command)
    # handle() returns (state, events, completed_event); the point of the test is
    # that the adapter invoked it without raising the correlation_id TypeError.
    assert result is not None


@pytest.mark.unit
def test_design_to_plan_handle_accepts_runtime_dispatch() -> None:
    """Reproduce OMN-13711 through the real adapter for HandlerDesignToPlan."""
    from omnimarket.nodes.node_design_to_plan.handlers.handler_design_to_plan import (
        HandlerDesignToPlan,
        ModelDesignToPlanCommand,
    )

    command = ModelDesignToPlanCommand(
        correlation_id=uuid.uuid4(),
        topic="conformance probe",
        requested_at=datetime.now(tz=UTC),
        dry_run=True,
        plan_only=True,
    )
    result = _invoke_handle_method(HandlerDesignToPlan().handle, command)
    assert result is not None
