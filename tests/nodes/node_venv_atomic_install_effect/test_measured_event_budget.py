# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recorded strace reading of one real pass over a copy of the dispatch venv."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).with_name("measurement_omn22712.json")


def test_one_pass_over_a_37k_file_venv_makes_at_most_40000_events() -> None:
    reading = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert reading["venv_copy"]["files"] >= 37000
    assert reading["new_pass"]["status"] == "installed"
    assert reading["new_pass"]["total_syscalls"] <= reading["limit"] == 40000
    assert (
        reading["new_pass"]["total_syscalls"] < reading["recorded_before_file_events"]
    )


def test_the_reading_was_taken_of_the_pass_this_tree_carries() -> None:
    from importlib import import_module

    reading = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert len(reading["measured_code_commit"]) == 40
    handler = import_module(
        "omnimarket.nodes.node_venv_atomic_install_effect.handlers.handler_venv_atomic_install"
    )
    assert hasattr(handler, "__file__")


def test_ac4_the_node_keeps_the_canonical_file_shape_with_no_baseline_entry() -> None:
    from importlib import import_module

    pkg = import_module("omnimarket.nodes.node_venv_atomic_install_effect")
    node_dir = Path(pkg.__file__).parent
    assert (node_dir / "contract.yaml").is_file()
    assert (node_dir / "metadata.yaml").is_file()
    assert (node_dir / "runtime_venv_atomic_install.py").is_file()
    assert not list(node_dir.glob("cli_*.py"))
