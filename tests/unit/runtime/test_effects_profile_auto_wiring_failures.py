# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression harness for the five effects-lane auto-wiring failures (OMN-13158).

Background
----------
After the Codex OAuth delegation repair the ``.201`` ``omninode-runtime-effects``
container booted healthy but the auto-wiring summary reported::

    Auto-wiring: 116 wired, 1 skipped, 5 failed

The five failed contracts (all in omnimarket, all in the ``effects`` runtime
profile) were:

  F1 ``adr_segmentation_llm_effect``  -> ``HandlerSegmentation``
       ``resolve_api_key() is sync-only; call resolve_api_key_async() from an
       async context`` raised because the sync constructor resolves a secret
       while the runtime is already inside the auto-wiring event loop.
  F2 ``build_loop_orchestrator``      -> ``AdapterDelegationRouter``
       contract declares the symbol, module does not export it.
  F3 ``dispatch_outcome_bridge_effect`` -> ``HandlerDispatchOutcomeBridge``
       contract ``db_io.database: omniintelligence`` rejected by the infra DB
       projection validator (``Unknown database 'omniintelligence'``).
  F4 ``overnight``                    -> ``OverseerTick``
       contract declares the symbol, module does not export it.
  F5 ``platform_diagnostics``         -> ``dimension_checks``
       contract declares the symbol, module does not export it.

Strategy
--------
This harness reproduces each failure against the **exact infra auto-wiring code
surfaces** the effects runtime uses, without booting the full
``wire_from_manifest`` path. Driving the full prepare path from file-path
discovery is NOT faithful here for two reasons: (1) ``discover_contracts_from_paths``
sets ``package_name="local"`` so the local-ownership query treats every node as
foreign and skips it; and (2) the resolver raises a boot-fatal ``TypeError`` for
the orchestrator's legitimate ``event_bus`` constructor param before reaching
the target failures, because no container / event bus is wired in a unit test.
The production runtime supplies both, so a unit test that wants to reproduce
F1..F5 must hit the specific code each failure originates from:

  * Layer A — targeted per-failure reproductions against the real infra code:
      - F1: construct ``HandlerSegmentation`` *inside a running event loop*
        (``asyncio_mode = "auto"``), which is what triggers the sync
        ``resolve_api_key()`` rejection the runtime hits during async boot.
      - F3: the ``db_io.database: omniintelligence`` acceptance is an infra
        validator concern (``_DB_URL_ENV_MAP`` in ``handler_wiring``), so it is
        regression-tested in omnibase_infra
        (``tests/unit/runtime/auto_wiring/test_handler_wiring_db_injection.py``)
        rather than here — this omnimarket harness would otherwise couple to the
        infra release / pin bump. Layer B below still proves the
        ``dispatch_outcome_bridge_effect`` handler symbol resolves.
      - F2/F4/F5: assert the declared handler symbol exists and is
        handler-shaped (named wrappers below; the same check Layer B applies).

  * Layer B — the generalized invariant the plan calls for (§5):
    for EVERY entry in each of the five contracts' ``handler_routing.handlers``,
    the module imports, the declared symbol exists, and the symbol satisfies
    the handler shape auto-wiring expects (a class exposing
    ``handle``/``handle_async``). This is transport-independent and is the
    permanent contract-validator shape; it reproduces F2/F4/F5 directly
    (missing symbol -> assertion) and also flags helper functions mis-listed as
    auto-wired handlers.

Red baseline: on current ``dev`` code every assertion below fails because the
five failures are still present. The fix PR turns them green.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from pathlib import Path

import pytest
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths

# ---------------------------------------------------------------------------
# Contract discovery
# ---------------------------------------------------------------------------

# Repo root: tests/unit/runtime/<this file> -> parents[3] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_NODES_DIR = _REPO_ROOT / "src" / "omnimarket" / "nodes"

# Map the deploy-log contract name -> the node directory that owns its
# contract.yaml. The directory name is NOT always the contract name (e.g.
# "overnight" lives in node_overnight, not node_overnight_<x>), so this mapping
# is explicit rather than derived.
_CONTRACT_NODE_DIRS: dict[str, str] = {
    "adr_segmentation_llm_effect": "node_adr_segmentation_llm_effect",
    "build_loop_orchestrator": "node_build_loop_orchestrator",
    "dispatch_outcome_bridge_effect": "node_dispatch_outcome_bridge_effect",
    "overnight": "node_overnight",
    "platform_diagnostics": "node_platform_diagnostics",
}


def _contract_path(contract_name: str) -> Path:
    path = _NODES_DIR / _CONTRACT_NODE_DIRS[contract_name] / "contract.yaml"
    if not path.is_file():
        raise FileNotFoundError(
            f"Contract for {contract_name!r} not found at {path} — the harness "
            "node-dir mapping is stale."
        )
    return path


_CONTRACT_PATHS: list[Path] = [_contract_path(name) for name in _CONTRACT_NODE_DIRS]


def _discover() -> object:
    """Discover the five contracts via the real auto-wiring discovery API."""
    manifest = discover_contracts_from_paths(list(_CONTRACT_PATHS))
    discovered = {c.name for c in manifest.contracts}
    expected = set(_CONTRACT_NODE_DIRS)
    missing = expected - discovered
    if missing:
        raise AssertionError(
            f"discovery did not yield expected contracts {sorted(missing)}; "
            f"discovered={sorted(discovered)}; errors={manifest.errors}"
        )
    return manifest


# ---------------------------------------------------------------------------
# Shared per-entry helpers (used by both layers)
# ---------------------------------------------------------------------------


def _iter_routing_entries() -> list[tuple[str, str, str]]:
    """Yield (contract_name, module_path, symbol_name) for every routing entry."""
    manifest = _discover()
    entries: list[tuple[str, str, str]] = []
    for contract in manifest.contracts:  # type: ignore[attr-defined]
        routing = contract.handler_routing
        if routing is None:
            continue
        for entry in routing.handlers:
            entries.append((contract.name, entry.handler.module, entry.handler.name))
    return entries


_ROUTING_ENTRIES = _iter_routing_entries()


def _symbol_is_handler_shaped(symbol: object) -> bool:
    """True when ``symbol`` satisfies the auto-wiring handler shape.

    Auto-wiring builds a dispatch callback that calls ``handle`` /
    ``handle_async`` on a constructed instance, so a handler entry must resolve
    to a class exposing one of those methods. A bare module-level function /
    helper (the F2/F4/F5 failure shape) does not qualify.
    """
    if inspect.isclass(symbol):
        return any(
            callable(getattr(symbol, attr, None)) for attr in ("handle", "handle_async")
        )
    return False


def _assert_routing_entry_resolves_to_handler(contract_name: str) -> None:
    """Assert every routing entry of ``contract_name`` resolves to a real handler.

    Shared by the named F2/F4/F5 wrappers and the parametrized Layer-B test.
    Reproduces a missing/stale declared symbol or a helper mis-listed as a
    handler.
    """
    entries = [e for e in _ROUTING_ENTRIES if e[0] == contract_name]
    assert entries, f"no handler_routing entries discovered for {contract_name!r}"
    for _name, module_path, symbol_name in entries:
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:
            pytest.fail(
                f"{contract_name}: handler module {module_path!r} failed to "
                f"import: {type(exc).__name__}: {exc}"
            )
        symbol = getattr(module, symbol_name, None)
        assert symbol is not None, (
            f"{contract_name}: handler_routing declares symbol {symbol_name!r} "
            f"but module {module_path!r} does not export it. Either the symbol "
            "name is stale (remove the entry / point it at the real handler) or "
            "a helper is mis-listed as an auto-wired handler."
        )
        assert _symbol_is_handler_shaped(symbol), (
            f"{contract_name}: declared handler {module_path}.{symbol_name} is "
            "not a class exposing handle()/handle_async() — auto-wiring cannot "
            "dispatch to it. A module-level helper must not be listed under "
            "handler_routing.handlers."
        )


# ---------------------------------------------------------------------------
# Layer A: targeted per-failure reproductions against the real infra code.
# ---------------------------------------------------------------------------


async def test_f1_adr_segmentation_constructs_in_async_context() -> None:
    """F1: HandlerSegmentation() must construct inside a running event loop.

    The runtime auto-wires handlers while already inside an event loop. The
    current sync ``HandlerSegmentation.__init__`` resolves a secret via the
    sync ``resolve_api_key()``, which deterministically raises
    ``RuntimeError("resolve_api_key() is sync-only ...")`` when a loop is
    running. Constructing it here (this test is async, so a loop is live)
    reproduces the F1 boot failure.
    """
    # Imported lazily so a collection-time import error does not mask the
    # construction-time failure this test exists to observe.
    from omnimarket.nodes.node_adr_segmentation_llm_effect.handlers.handler_segmentation import (
        HandlerSegmentation,
    )

    assert asyncio.get_running_loop() is not None  # precondition: loop is live
    try:
        HandlerSegmentation()
    except RuntimeError as exc:  # pragma: no cover - asserted below
        pytest.fail(
            "F1: HandlerSegmentation() raised inside an async context: "
            f"{exc}. The constructor must not call the sync resolve_api_key() "
            "from the async auto-wiring path (defer to resolve_api_key_async / "
            "a concurrency-safe lazy bridge)."
        )


def test_f2_build_loop_orchestrator_handlers_resolve() -> None:
    """F2: every build_loop_orchestrator routing entry must resolve to a handler.

    Reproduces the stale ``AdapterDelegationRouter`` declaration (module does
    not export that class).
    """
    _assert_routing_entry_resolves_to_handler("build_loop_orchestrator")


# F3 (dispatch_outcome_bridge_effect db_io.database == "omniintelligence") is an
# infra-validator concern, not an omnimarket-owned one: acceptance is enforced by
# omnibase_infra's ``_DB_URL_ENV_MAP`` and regression-tested there
# (tests/unit/runtime/auto_wiring/test_handler_wiring_db_injection.py ::
# test_omniintelligence_is_accepted_db_identity_per_db_url_contract, OMN-13158).
# omnimarket's pinned omnibase_infra does not carry that fix until the pin
# propagates, so asserting it here would couple this PR to the infra release.
# Layer B below still proves the dispatch_outcome_bridge_effect *handler* symbol
# resolves; the db_io acceptance lives with the validator that owns it.


def test_f4_overnight_handlers_resolve() -> None:
    """F4: every overnight routing entry must resolve to a handler.

    Reproduces the stale ``OverseerTick`` declaration (helper module does not
    export that class).
    """
    _assert_routing_entry_resolves_to_handler("overnight")


def test_f5_platform_diagnostics_handlers_resolve() -> None:
    """F5: every platform_diagnostics routing entry must resolve to a handler.

    Reproduces the ``dimension_checks`` helper module mis-listed as a handler
    (no attribute by that name; the real handler is HandlerPlatformDiagnostics).
    """
    _assert_routing_entry_resolves_to_handler("platform_diagnostics")


# ---------------------------------------------------------------------------
# Layer B: generalized per-entry import + symbol + handler-shape invariant.
#
# Transport-independent: this is the permanent contract-validator shape the
# plan asks for (§5). For EVERY handler_routing entry in EVERY one of the five
# contracts, the module must import, the declared symbol must exist, and the
# symbol must be an auto-wirable handler (a class exposing handle /
# handle_async). Reproduces F2/F4/F5 directly, independent of the prepare API.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("contract_name", "module_path", "symbol_name"),
    _ROUTING_ENTRIES,
    ids=[f"{c}::{n}" for c, _m, n in _ROUTING_ENTRIES],
)
def test_every_handler_routing_entry_imports_and_exports_a_handler(
    contract_name: str,
    module_path: str,
    symbol_name: str,
) -> None:
    """Every handler_routing entry must import, export its symbol, and be handler-shaped."""
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        pytest.fail(
            f"{contract_name}: handler module {module_path!r} failed to import: "
            f"{type(exc).__name__}: {exc}"
        )

    symbol = getattr(module, symbol_name, None)
    assert symbol is not None, (
        f"{contract_name}: handler_routing declares symbol {symbol_name!r} but "
        f"module {module_path!r} does not export it. Either the symbol name is "
        "stale (remove the entry / point it at the real handler) or the helper "
        "is mis-listed as an auto-wired handler."
    )

    assert _symbol_is_handler_shaped(symbol), (
        f"{contract_name}: declared handler {module_path}.{symbol_name} is not a "
        "class exposing handle()/handle_async() — auto-wiring cannot dispatch to "
        "it. A module-level helper function must not be listed under "
        "handler_routing.handlers."
    )
