# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Core-only-install guarantee harness (OMN-13446, Phase 5, epic OMN-13442).

Local-First Runtime Re-Convergence: RuntimeLocal + the local-runtime protocols
are now CORE-RESIDENT (``omnibase_core.runtime`` / ``omnibase_core.protocols.runtime``,
relocated from omnibase_infra in OMN-13444 and deleted from infra in OMN-13445).
A clean local install must therefore be runnable with ``omnibase-infra`` UNINSTALLED.

This harness is the executable proof of that guarantee. It is run by the
``core-only-install`` CI job in a clean venv where ``omnibase-infra`` is NOT
installed (Rule 5: enforcement, not detection). It asserts, in order:

1. ``omnibase-infra`` is genuinely absent (the gate is meaningless if infra leaked in).
2. ``import omnibase_core.runtime`` succeeds and exposes ``RuntimeLocal``.
3. The in-memory RuntimeLocal harness is RUNNABLE end-to-end with the core-resident
   ``EventBusInmemory`` — i.e. it executes a real contract-declared workflow
   (``node_merge_sweep_compute``) to ``COMPLETED`` with zero infra on the path.

Exit code 0 == guarantee holds; any non-zero exit fails the CI job.

Run locally (mirrors the gate) from a core-only venv with ``PYTHONPATH=src``:

    PYTHONPATH=src python scripts/ci/core_only_install_harness.py
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
from pathlib import Path

# Resolve the merge-sweep workflow contract relative to the repo root. This file
# lives at <repo>/scripts/ci/, so the repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT_PATH = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_merge_sweep_compute"
    / "contract.yaml"
)


def _fail(message: str) -> None:
    """Print a CI-annotated error and exit non-zero."""
    print(f"::error::core-only-install: {message}")
    print("FAIL")
    raise SystemExit(1)


def _assert_infra_absent() -> None:
    """The gate is only meaningful if omnibase_infra is genuinely uninstalled."""
    if importlib.util.find_spec("omnibase_infra") is not None:
        _fail(
            "omnibase_infra IS importable in this environment — the core-only "
            "guarantee cannot be proven. Uninstall omnibase-infra before running "
            "the core-only-install gate."
        )
    print("OK: omnibase_infra is NOT installed (core-only environment confirmed)")


def _assert_core_runtime_imports() -> type:
    """import omnibase_core.runtime + RuntimeLocal must resolve from CORE."""
    try:
        runtime_pkg = importlib.import_module("omnibase_core.runtime")
    except ImportError as exc:  # pragma: no cover - exercised by the CI gate
        _fail(f"`import omnibase_core.runtime` failed: {exc!r}")

    runtime_local_class = getattr(runtime_pkg, "RuntimeLocal", None)
    if runtime_local_class is None:
        _fail("omnibase_core.runtime does not export RuntimeLocal")

    # Prove the symbol resolves from the core package (not a leaked infra alias).
    module_name = runtime_local_class.__module__
    if not module_name.startswith("omnibase_core."):
        _fail(
            "RuntimeLocal resolves from a non-core module "
            f"({module_name!r}); expected an omnibase_core.* module"
        )
    print(f"OK: omnibase_core.runtime.RuntimeLocal imports from {module_name}")
    return runtime_local_class


def _assert_inmemory_harness_runnable(runtime_local_class: type) -> None:
    """RuntimeLocal must execute a real contract to COMPLETED, infra-free."""
    from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

    if not _CONTRACT_PATH.exists():
        _fail(f"workflow contract missing at {_CONTRACT_PATH}")

    with tempfile.TemporaryDirectory(prefix="omn13446-core-only-") as tmp:
        state_root = Path(tmp) / "state"
        runtime = runtime_local_class(
            workflow_path=_CONTRACT_PATH,
            state_root=state_root,
            timeout=60,
        )
        result = runtime.run()

        if result != EnumWorkflowResult.COMPLETED:
            _fail(
                "in-memory RuntimeLocal harness did not COMPLETE "
                f"(result={result!r}, last_error={runtime.last_error!r})"
            )
        if runtime.exit_code != 0:
            _fail(f"harness exit_code={runtime.exit_code} (expected 0)")

        state_file = state_root / "workflow_result.json"
        if not state_file.exists():
            _fail(f"workflow_result.json missing at {state_file}")

    print(
        "OK: in-memory RuntimeLocal harness ran node_merge_sweep_compute to "
        "COMPLETED with the core-resident EventBusInmemory (zero infra)"
    )


def main() -> int:
    print("=== OMN-13446 core-only-install guarantee harness ===")
    print(f"python: {sys.version.split()[0]}")
    _assert_infra_absent()
    runtime_local_class = _assert_core_runtime_imports()
    _assert_inmemory_harness_runnable(runtime_local_class)
    print("PASS: core-only-install guarantee holds (RuntimeLocal core-resident)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
