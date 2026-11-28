# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25958 AC3: the release-cut gate does not list CHANGELOG.md as a cut path.

The release train reads this repository's own release-cut gate from the checkout
(the ``_RELEASE_CUT_PATHS`` literal in the ticket-contract prerequisite handler)
and writes ``CHANGELOG.md`` into a cut for as long as the gate lists it. Each cut
then re-adds the file the removed-prose test forbids on dev. A gate that lists no
changelog makes the train carry the notes in the release pull request body.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
GATE = (
    ROOT
    / "src/omnimarket/nodes/node_ticket_contract_prerequisite_effect/handlers"
    / "handler_ticket_contract_prerequisite.py"
)


def _release_cut_path_literals() -> set[str]:
    """The string constants under the ``_RELEASE_CUT_PATHS`` assignment, as the train reads them."""
    tree = ast.parse(GATE.read_text(encoding="utf-8"))
    leaves: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_RELEASE_CUT_PATHS"
            for target in node.targets
        ):
            leaves |= {
                leaf.value
                for leaf in ast.walk(node.value)
                if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str)
            }
    return leaves


def test_release_cut_gate_lists_the_version_files() -> None:
    """Positive control: the probe reads the real assignment."""
    assert {"pyproject.toml", "uv.lock"} <= _release_cut_path_literals()


def test_release_cut_gate_does_not_list_changelog() -> None:
    assert "CHANGELOG.md" not in _release_cut_path_literals()
