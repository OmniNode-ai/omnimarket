# SPDX-License-Identifier: MIT
"""OMN-13984: core PR-lifecycle sub-handlers must fail-fast on ImportError.

Regression guard for a silent-degradation facade. A broken or partial
``omnimarket`` install (packaging / co-install drift — the OMN-13829/13930
class that is known to recur) must NOT let the merge-sweep orchestrator quietly
swap in a no-op stub that returns 0 PRs / 0 merges / 0 fixes and reports a
"successful" sweep. Each of the 5 core sub-handlers
(inventory / triage / reducer / merge / fix) must RAISE when its real handler
cannot be imported.

Worktree-prune (OMN-13859) is the one intentional optional — GC failing must
never fail a sweep — so it keeps its stub fallback and is deliberately excluded
here.
"""

from __future__ import annotations

import sys
from unittest.mock import Mock

import pytest

from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
)

# (sub-handler name, first module imported inside that handler's try-block).
# Setting the module to ``None`` in sys.modules makes its ``from ... import``
# raise ImportError — simulating packaging/install drift for exactly one
# sub-handler while the others import normally.
_CORE_SUBHANDLERS: list[tuple[str, str]] = [
    (
        "inventory",
        "omnimarket.nodes.node_pr_lifecycle_inventory_compute.handlers.handler_pr_lifecycle_inventory",
    ),
    (
        "triage",
        "omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_pr_lifecycle_triage",
    ),
    (
        "reducer",
        "omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer",
    ),
    (
        "merge",
        "omnimarket.nodes.node_pr_lifecycle_merge_effect.handlers.adapter_github_merge_queue",
    ),
    (
        "fix",
        "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_delegated_fix",
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize(("name", "broken_module"), _CORE_SUBHANDLERS)
def test_core_subhandler_import_failure_raises_not_silent_stub(
    name: str,
    broken_module: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ImportError on a core sub-handler must fail loud, never a silent stub.

    Before OMN-13984 this returned a ``_Stub*Handler`` and the sweep reported
    success while doing nothing. Now it must raise RuntimeError.
    """
    # Force the real handler's import to fail (simulated packaging drift).
    monkeypatch.setitem(sys.modules, broken_module, None)

    handler = HandlerPrLifecycleOrchestrator(event_bus=Mock())

    with pytest.raises(RuntimeError, match=r"refusing to run|failed to import"):
        handler._ensure_sub_handlers()


@pytest.mark.unit
def test_healthy_install_wires_real_handlers_no_stub() -> None:
    """With a healthy install, _ensure_sub_handlers wires real handlers, no stub.

    Positive control: proves the fail-fast change did not break the happy path —
    every core sub-handler resolves to its real implementation, never a
    ``_Stub*Handler``.
    """
    handler = HandlerPrLifecycleOrchestrator(event_bus=Mock())
    handler._ensure_sub_handlers()

    for attr in ("_inventory", "_triage", "_reducer", "_merge", "_fix"):
        wired = getattr(handler, attr)
        assert wired is not None, f"{attr} was not wired"
        assert "Stub" not in type(wired).__name__, (
            f"{attr} resolved to a stub ({type(wired).__name__}) on a healthy "
            "install — the ImportError fallback must not fire here"
        )
