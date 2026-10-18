# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21136: the retired context-bundle generator node leaves no trace in the tree.

The names are assembled from fragments so this file does not match its own scan.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
_NODE = "node_context_" + "bundle_generator_compute"
_NAMES = (
    _NODE,
    "context-bundle-" + "requested",
    "context-bundle-" + "completed",
    "handler_context_" + "bundle",
)
_PATTERN = re.compile("|".join(re.escape(name) for name in _NAMES))
_TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".toml", ".sh", ".md"}


def _matches(*relative: str) -> list[str]:
    hits: list[str] = []
    for entry in relative:
        base = ROOT / entry
        files = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in files:
            if (
                path.is_file()
                and path.suffix in _TEXT_SUFFIXES
                and "__pycache__" not in path.parts
                and _PATTERN.search(path.read_text(encoding="utf-8", errors="replace"))
            ):
                hits.append(str(path.relative_to(ROOT)))
    return hits


def test_scan_positive_control_finds_a_planted_reference() -> None:
    assert _PATTERN.search(f"import {_NODE}")
    assert _PATTERN.search("topic: onex.cmd.omnimarket." + _NAMES[1] + ".v1")
    assert not _PATTERN.search("node_context_pack_render_compute")


def test_no_caller_importer_subscriber_or_publisher_remains_in_the_tree() -> None:
    assert _matches("src", "tests", "scripts") == []


def test_the_node_package_is_absent_and_no_config_names_it() -> None:
    node_dir = ROOT / "src" / "omnimarket" / "nodes" / _NODE
    assert not (node_dir / "contract.yaml").exists()
    assert not (node_dir / "handlers").exists()
    assert (
        _matches("config", "pyproject.toml", "imperative-contract-allowlist.yaml") == []
    )
