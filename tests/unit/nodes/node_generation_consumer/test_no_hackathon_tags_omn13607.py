# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13607 (WS-C Phase 0.3): node_generation_consumer carries no hackathon tags.

The node is a permanent canonical artifact, not a hackathon prototype. The SEA
canonicalization mandate (epic OMN-13604, plan §Phase 0.3) requires every
hackathon-era marker, tag, and label to be stripped from the node so the surface
reads as permanent and canonical.

DoD (plan doc §Phase 0.3):
  * ``grep -r "hackathon" src/omnimarket/nodes/node_generation_consumer/`` returns empty
  * tests pass
  * contract and metadata reflect canonical node identity

This test pins the grep-clean invariant mechanically so the marker cannot creep
back in. It scans the whole node source tree (contract.yaml, metadata.yaml,
handlers, models, validator corpora) case-insensitively.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_generation_consumer"
)

# Files the scanner skips: compiled caches only. Every authored source file is in
# scope -- the DoD grep is unfiltered over the node directory.
_SKIP_DIR_NAMES = frozenset({"__pycache__"})


def _node_source_files() -> list[Path]:
    return sorted(
        p
        for p in _NODE_DIR.rglob("*")
        if p.is_file() and not any(part in _SKIP_DIR_NAMES for part in p.parts)
    )


@pytest.mark.unit
def test_node_dir_exists() -> None:
    """Guard: the scanned node directory must exist (fail fast if relocated)."""
    assert _NODE_DIR.is_dir(), f"node directory not found: {_NODE_DIR}"


@pytest.mark.unit
def test_no_hackathon_marker_anywhere_in_node_source() -> None:
    """No file under the node directory may contain the 'hackathon' marker.

    Mirrors the plan-doc DoD grep, case-insensitively, across every authored
    source file in the node tree.
    """
    offenders: list[str] = []
    for path in _node_source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # binary fixture -- cannot carry a textual marker
            continue
        if "hackathon" in text.lower():
            rel = path.relative_to(_NODE_DIR)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "hackathon" in line.lower():
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "hackathon marker found in node_generation_consumer source "
        "(plan section Phase 0.3 requires grep-clean):\n" + "\n".join(offenders)
    )


@pytest.mark.unit
def test_contract_tags_have_no_hackathon() -> None:
    """contract.yaml metadata.tags must not list 'hackathon'."""
    contract = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    tags = contract.get("metadata", {}).get("tags", [])
    assert "hackathon" not in tags, (
        f"contract.yaml metadata.tags still lists hackathon: {tags}"
    )


@pytest.mark.unit
def test_metadata_tags_have_no_hackathon() -> None:
    """metadata.yaml tags must not list 'hackathon'."""
    metadata = yaml.safe_load((_NODE_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    tags = metadata.get("tags", [])
    assert "hackathon" not in tags, f"metadata.yaml tags still list hackathon: {tags}"
