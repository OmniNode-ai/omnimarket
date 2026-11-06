# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evidence for the remaining routing package relocation slice."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_MOVED = (
    "dod_overlay",
    "roi_overlay",
    "tenant_overlay_resolver",
    "generated_llm_routing_constants",
)


@pytest.mark.parametrize("module", _MOVED)
def test_remaining_routing_helpers_are_canonical(module: str) -> None:
    loaded = importlib.import_module(f"omnimarket.handlers.routing.{module}")
    assert (
        Path(loaded.__file__).resolve()
        == _ROOT / "src/omnimarket/handlers/routing" / f"{module}.py"
    )
    assert not (_ROOT / "src/omnimarket/routing" / f"{module}.py").exists()


def test_legacy_routing_package_and_baseline_entries_are_removed() -> None:
    assert not (_ROOT / "src/omnimarket/routing/__init__.py").exists()
    baseline = (_ROOT / ".onex_ratchets/canonical_file_shape_baseline.txt").read_text()
    assert "src/omnimarket/routing/" not in baseline
