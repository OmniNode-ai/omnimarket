# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression guard (OMN-13550): every contract handler symbol must exist.

A dev-HEAD redeploy crash-looped the runtime at boot with
``CLASS_NOT_FOUND (HANDLER_LOADER_011)`` because
``node_baseline_capture/contract.yaml`` named a handler ``probe_db_row_counts``
that does not exist in its module — the real symbol is ``ProbeDbRowCounts``.
OMN-12408's fail-loud (merged 2026-06-23) correctly crashes on this instead of
silently skipping the contract, which blocked the whole dev runtime boot.

This test mirrors the runtime loader's ``hasattr(module, handler.name)`` check
(``omnibase_infra.runtime.contract_loaders.handler_routing_loader``) so the
defect class is caught in CI here, against the pinned omnibase_infra in this
repo's venv (which predates OMN-12408 and therefore does not enforce it).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

_NODES_DIR = Path(__file__).resolve().parent.parent / "src" / "omnimarket" / "nodes"


def _handler_entries() -> list[tuple[str, str, str]]:
    """Return (contract_relpath, handler_name, handler_module) for every entry."""
    entries: list[tuple[str, str, str]] = []
    for contract_path in sorted(_NODES_DIR.glob("**/contract.yaml")):
        data = yaml.safe_load(contract_path.read_text())
        if not isinstance(data, dict):
            continue
        handler_routing = data.get("handler_routing") or {}
        for entry in handler_routing.get("handlers") or []:
            handler = entry.get("handler") or {}
            name = handler.get("name")
            module = handler.get("module")
            if name and module:
                rel = str(contract_path.relative_to(_NODES_DIR.parent.parent.parent))
                entries.append((rel, name, module))
    return entries


_ENTRIES = _handler_entries()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("contract_rel", "handler_name", "handler_module"),
    _ENTRIES,
    ids=[f"{rel}::{name}" for rel, name, _ in _ENTRIES],
)
def test_handler_symbol_exists(
    contract_rel: str, handler_name: str, handler_module: str
) -> None:
    """The named handler symbol must exist in its declared module.

    Matches the runtime loader's fail-loud check (OMN-12408): a contract that
    names a handler symbol that does not exist is a build defect, not a
    degradable condition.
    """
    module = importlib.import_module(handler_module)
    assert hasattr(module, handler_name), (
        f"CLASS_NOT_FOUND: '{handler_name}' does not exist in module "
        f"'{handler_module}', declared in {contract_rel}. "
        f"Available: {sorted(n for n in dir(module) if not n.startswith('_'))}"
    )


@pytest.mark.unit
def test_at_least_one_handler_entry_scanned() -> None:
    """Guard against the parametrize list silently collapsing to empty."""
    assert _ENTRIES, "no handler_routing entries discovered — scan is broken"
