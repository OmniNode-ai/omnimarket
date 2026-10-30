# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21600: omnimarket reaches two core handlers at their node paths.

omnibase_core#2135 (OMN-21515) moved handler_done_write_receipt_gate into
node_done_write_receipt_gate_compute and handler_migration_conflicts into
node_cross_repo_boundary_check_effect, released as omnibase-core 0.47.58.
omnimarket still imported the old paths and pinned core from before the move,
so its own tests stayed green while the dev-lane image, which resolves the
moved core, failed auto-wiring with ModuleNotFoundError and both runtimes
failed boot. These tests import the two omnimarket modules under the pinned
core.
"""

from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.unit

MODULES = (
    "omnimarket.nodes.node_linear_triage.services.done_write_receipt_gate",
    "omnimarket.nodes.node_duplication_sweep.handlers.handler_duplication_sweep",
)


@pytest.mark.parametrize("module", MODULES)
def test_the_module_imports_under_the_pinned_core(module: str) -> None:
    importlib.import_module(module)


def test_the_pinned_core_no_longer_ships_the_old_handler_paths() -> None:
    for old in (
        "omnibase_core.handlers.handler_done_write_receipt_gate",
        "omnibase_core.handlers.handler_migration_conflicts",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old)
