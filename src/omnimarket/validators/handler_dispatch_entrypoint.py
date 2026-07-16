# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every contract-declared handler must expose a dispatch entrypoint (OMN-14617).

FLEET-WIDE FAN-OUT OF THE omnibase_infra OMN-14135 GATE
-------------------------------------------------------
This is the omnimarket instance of the dispatch-ENTRYPOINT gate that
``omnibase_infra.validators.handler_dispatch_entrypoint`` (OMN-14135 / OMN-14489)
runs against ``src/omnibase_infra``. That validator's ``DEFAULT_SCAN_ROOT`` only ever
scanned ``src/omnibase_infra``, so omnimarket (and omniintelligence / omnimemory) had
ZERO mechanical coverage for this defect class. ``node_pattern_b_broker``'s
``AdapterPatternBBrokerPublish`` / ``AdapterPatternBBrokerTerminalConsumer`` (omnimarket)
shipped contract-declared, wired, ingress-valid, and CI-green while exposing neither
``handle()`` nor ``handle_async()`` — the exact silent-``ModelOnexError``-at-first-dispatch
mechanism below — and were only caught by manual OMN-14605 triage (fixed separately under
OMN-14616). This module closes that seam in omnimarket.

It is a SELF-CONTAINED copy rather than an import of
``omnibase_infra.validators.handler_dispatch_entrypoint`` on purpose: that module is not
in any released ``omnibase-infra`` wheel yet (it lives on omnibase_infra ``dev``, not
``main``/PyPI), so a direct ``import`` would fail in omnimarket CI, which pip-installs the
released wheel. The predicate is deliberately tiny and matches omnimarket's own
``omnimarket.validators.event_registry_drift`` precedent (a repo-local validator module).

THE DEFECT CLASS THIS GATE CLOSES
---------------------------------
A handler can be contract-declared, auto-wired, instantiated, boot clean, and GREEN in CI
— and still have NO dispatch entrypoint at all. The shared runtime's
``_make_dispatch_callback`` (``omnibase_infra.runtime.auto_wiring.handler_wiring``)
resolves the runtime entrypoint by looking for ``handle_async``, then ``handle``. Finding
neither, it binds ``_missing_handle``, which raises ``ModelOnexError`` on the FIRST real
dispatch. The dispatcher still REGISTERS and the payload still ROUTES to it — so every
upstream signal (contract validation, auto-wiring, boot, route-coverage oracles) stays
green. "Registered" and "routable" are NOT "executable".

RATCHET SEMANTICS (shrink-only; see validation/handler_dispatch_entrypoint_baseline.yaml)
-----------------------------------------------------------------------------------------
  * a handler NOT in the baseline with no entrypoint  -> FAIL (no new instances, ever);
  * a handler IN the baseline that GAINS an entrypoint -> FAIL until it is removed from
    the baseline (a fixed handler still listed is STALE, so the list cannot rot).

The baseline can only shrink. End state is an empty list.

NON-VACUITY (OMN-14541 optional-census class defect)
----------------------------------------------------
A scan that discovers far fewer handlers than omnimarket actually has is a broken scan,
not a clean repo. A gate over an empty/collapsed set is vacuously green, so the validator
fails closed below ``MIN_EXPECTED_DECLARED_HANDLERS`` rather than reporting success.

Usage (pre-commit / CI):
    PYTHONPATH=src uv run python -m omnimarket.validators.handler_dispatch_entrypoint src/omnimarket
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_SCAN_ROOT = Path("src/omnimarket")
DEFAULT_BASELINE = Path("validation/handler_dispatch_entrypoint_baseline.yaml")

# omnimarket declares ~421 handler rows (380 distinct) across ~380 node contracts today.
# A scan that collapses well below that is broken, not clean, so the gate fails closed
# under this floor rather than passing vacuously. Kept conservatively below the live
# count to tolerate legitimate node churn while still catching a collapsed scan.
MIN_EXPECTED_DECLARED_HANDLERS = 300


@dataclass(frozen=True, slots=True)  # internal-dataclass-ok: validator-internal finding
class DeclaredDispatchTarget:
    """A dispatch target (handler class) named in a contract's ``handler_routing.handlers[]``."""

    contract: str
    handler: str
    module: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.contract, self.handler)


def has_dispatch_entrypoint(cls: type) -> bool:
    """The EXACT predicate ``_make_dispatch_callback`` uses to bind an entrypoint.

    Kept deliberately identical to the shared auto-wiring resolution order
    (``handle_async`` then ``handle``). If this drifts from ``handler_wiring``, the gate
    stops describing the runtime and starts describing itself.
    """
    return callable(getattr(cls, "handle_async", None)) or callable(
        getattr(cls, "handle", None)
    )


def declared_handlers(scan_root: Path) -> list[DeclaredDispatchTarget]:
    """Every handler declared in every ``contract.yaml`` under ``scan_root``."""
    found: list[DeclaredDispatchTarget] = []
    for contract_path in sorted(scan_root.rglob("contract.yaml")):
        data = yaml.safe_load(contract_path.read_text())
        if not isinstance(data, dict):
            continue
        contract = str(data.get("name") or contract_path.parent.name)
        routing = data.get("handler_routing") or {}
        for entry in routing.get("handlers") or []:
            handler = (entry or {}).get("handler") or {}
            name, module = handler.get("name"), handler.get("module")
            if name and module:
                found.append(DeclaredDispatchTarget(contract, str(name), str(module)))
    return found


def entrypointless(handlers: Sequence[DeclaredDispatchTarget]) -> set[tuple[str, str]]:
    """Declared handlers that expose NEITHER ``handle`` nor ``handle_async``.

    An unimportable handler is skipped: import health is a separate gate, and failing
    here would misattribute an import error to a missing entrypoint.
    """
    missing: set[tuple[str, str]] = set()
    for declared in handlers:
        try:
            cls = getattr(importlib.import_module(declared.module), declared.handler)
        except Exception:  # import health is a separate gate; do not misattribute here
            continue
        if not has_dispatch_entrypoint(cls):
            missing.add(declared.key)
    return missing


def load_baseline(baseline_path: Path) -> set[tuple[str, str]]:
    """Load the frozen shrink-only burn-down baseline."""
    if not baseline_path.is_file():
        return set()
    data = yaml.safe_load(baseline_path.read_text()) or {}
    return {
        (str(row["contract"]), str(row["handler"]))
        for row in (data.get("known_entrypointless") or [])
    }


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fail any contract-declared omnimarket handler that auto-wiring would bind "
            "to _missing_handle (no handle/handle_async dispatch entrypoint)."
        )
    )
    parser.add_argument(
        "scan_root",
        nargs="?",
        default=str(DEFAULT_SCAN_ROOT),
        help="Root to scan for contract.yaml files.",
    )
    parser.add_argument(
        "--baseline",
        default=str(DEFAULT_BASELINE),
        help="Frozen shrink-only burn-down baseline.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    scan_root, baseline_path = Path(args.scan_root), Path(args.baseline)

    handlers = declared_handlers(scan_root)
    if len(handlers) < MIN_EXPECTED_DECLARED_HANDLERS:
        sys.stderr.write(
            f"[handler-dispatch-entrypoint] FAIL (vacuity guard): only {len(handlers)} "
            f"contract-declared handlers found under {scan_root} (expected >= "
            f"{MIN_EXPECTED_DECLARED_HANDLERS}). The contract scan is broken; a gate "
            f"over an empty set proves nothing.\n"
        )
        return 1

    baseline = load_baseline(baseline_path)
    live = entrypointless(handlers)

    violations = sorted(live - baseline)
    stale = sorted(baseline - live)
    exit_code = 0

    if violations:
        exit_code = 1
        sys.stderr.write(
            "[handler-dispatch-entrypoint] FAIL: contract-declared handler(s) expose "
            "NEITHER handle() nor handle_async(). Auto-wiring binds these to "
            "_missing_handle, so EVERY dispatch raises ModelOnexError at runtime while "
            "CI stays green:\n"
        )
        for contract, handler in violations:
            sys.stderr.write(f"  - {contract}: {handler}\n")
        sys.stderr.write(
            "\n  Add a def-B `handle(request) -> response` entrypoint. Do NOT add the "
            f"handler to {baseline_path} — that baseline is frozen and shrink-only.\n"
        )

    if stale:
        exit_code = 1
        sys.stderr.write(
            f"[handler-dispatch-entrypoint] FAIL: handler(s) now HAVE a dispatch "
            f"entrypoint but are still listed in {baseline_path}. Remove them; the "
            f"baseline is shrink-only and must never go stale:\n"
        )
        for contract, handler in stale:
            sys.stderr.write(f"  - {contract}: {handler}\n")

    if exit_code == 0:
        distinct = len({declared.key for declared in handlers})
        sys.stderr.write(
            f"[handler-dispatch-entrypoint] OK: {distinct} distinct contract-declared "
            f"handlers ({len(handlers)} declaration rows), {len(live)} entrypointless "
            f"(all in the frozen baseline), 0 new violations.\n"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
