"""Phase 1 Task 3 (OMN-10202): pattern doc structural assertions.

Locks the canonical skill-backing node handler shape so every migrated
backing node references a single authoritative pattern document.
"""

from __future__ import annotations

from pathlib import Path

_DOC_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "patterns"
    / "skill_backing_node_pattern.md"
)

_REQUIRED = (
    "## Handler contract",
    "## Required input model fields",
    "## Required output model fields",
    "## Dispatch record persistence",
    "ModelDispatchRecord",
    "$ONEX_STATE_DIR/dispatches/",
    "## Why the handler does NOT call Agent()",
    "proposed_agent_spawn_args",
)


def test_pattern_doc_exists_and_declares_required_sections() -> None:
    doc = _DOC_PATH.read_text()
    for required in _REQUIRED:
        assert required in doc, f"pattern doc missing: {required}"
