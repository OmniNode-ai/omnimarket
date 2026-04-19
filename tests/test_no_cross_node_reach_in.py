# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract test: no new cross-node model reach-ins (OMN-9263).

A "reach-in" is when node_A imports from node_B's internal models package.
All new shared event models must live in omnimarket.events.* — never in a
sibling node's models package.

KNOWN_VIOLATIONS below is a frozen allowlist of pre-existing reach-ins that
predate this refactor. It is NOT a free pass — these must be fixed in
follow-up tickets. Adding a new entry here requires a Linear ticket reference.

This test FAILS if any reach-in is introduced that is NOT in the allowlist.
Ledger reach-ins were removed by OMN-9263 and are not in the allowlist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).parent.parent / "src"

_REACH_IN_PATTERN = re.compile(
    r"from\s+omnimarket\.nodes\.(node_[^.]+)\..*models.*import",
)

# Pre-existing reach-ins that predate OMN-9263. Each must be resolved in a
# follow-up ticket. Format: "relative/path/from/src:lineno".
# DO NOT add new entries here without a Linear ticket.
_KNOWN_VIOLATIONS: frozenset[str] = frozenset(
    [
        # node_build_loop_orchestrator → node_build_loop (multiple tickets pending)
        "omnimarket/nodes/node_build_loop_orchestrator/assemble_live.py:43",
        "omnimarket/nodes/node_build_loop_orchestrator/__main__.py:26",
        "omnimarket/nodes/node_build_loop_orchestrator/models/model_phase_command_intent.py:10",
        "omnimarket/nodes/node_build_loop_orchestrator/models/model_loop_cycle_summary.py:15",
        "omnimarket/nodes/node_build_loop_orchestrator/models/model_orchestrator_state.py:10",
        "omnimarket/nodes/node_build_loop_orchestrator/handlers/assemble_live.py:38",
        "omnimarket/nodes/node_build_loop_orchestrator/handlers/handler_orchestrator.py:16",
        "omnimarket/nodes/node_build_loop_orchestrator/handlers/handler_orchestrator.py:19",
        "omnimarket/nodes/node_build_loop_orchestrator/handlers/handler_build_loop_orchestrator.py:50",
        "omnimarket/nodes/node_build_loop_orchestrator/handlers/handler_build_loop_orchestrator.py:53",
        "omnimarket/nodes/node_build_loop_orchestrator/handlers/handler_build_loop_orchestrator.py:75",
        "omnimarket/nodes/node_build_loop_orchestrator/handlers/handler_build_loop_orchestrator.py:654",
        # node_pr_lifecycle_fix_effect → node_pr_lifecycle_inventory_compute
        "omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/handler_admin_merge.py:26",
        # node_baseline_compare → node_baseline_capture
        "omnimarket/nodes/node_baseline_compare/models/__init__.py:3",
        "omnimarket/nodes/node_baseline_compare/handlers/handler_baseline_compare.py:23",
        # node_intent_event_consumer_effect → node_intent_storage_effect
        "omnimarket/nodes/node_intent_event_consumer_effect/utils/util_event_mapper.py:15",
        # node_pr_review_bot → node_hostile_reviewer
        "omnimarket/nodes/node_pr_review_bot/models/models.py:20",
        # merge_sweep cluster reach-ins
        "omnimarket/nodes/node_merge_sweep_auto_merge_arm_effect/handlers/handler_auto_merge_arm.py:25",
        "omnimarket/nodes/node_ci_rerun_effect/handlers/handler_ci_rerun.py:24",
        "omnimarket/nodes/node_thread_reply_effect/handlers/handler_thread_reply.py:34",
        "omnimarket/nodes/node_rebase_effect/handlers/handler_rebase.py:33",
        "omnimarket/nodes/node_conflict_hunk_effect/handlers/handler_conflict_hunk.py:36",
        # node_ledger_append_effect → node_ledger_orchestrator (command model, not event)
        "omnimarket/nodes/node_ledger_append_effect/handlers/handler_ledger_append.py:25",
        # node_pr_lifecycle_orchestrator reach-ins
        "omnimarket/nodes/node_pr_lifecycle_orchestrator/handlers/handler_pr_lifecycle_orchestrator.py:869",
        "omnimarket/nodes/node_pr_lifecycle_orchestrator/handlers/handler_pr_lifecycle_orchestrator.py:922",
        "omnimarket/nodes/node_pr_lifecycle_orchestrator/handlers/handler_pr_lifecycle_orchestrator.py:991",
        "omnimarket/nodes/node_pr_lifecycle_orchestrator/handlers/handler_pr_lifecycle_orchestrator.py:1053",
        # node_overnight → node_build_loop
        "omnimarket/nodes/node_overnight/handlers/handler_overnight.py:1071",
        # node_pipeline_fill → node_rsd_fill_compute
        "omnimarket/nodes/node_pipeline_fill/handlers/handler_pipeline_fill.py:40",
        # node_merge_sweep_state_reducer reach-ins
        "omnimarket/nodes/node_merge_sweep_state_reducer/models/model_merge_sweep_state.py:17",
        "omnimarket/nodes/node_merge_sweep_state_reducer/handlers/handler_sweep_state.py:52",
        "omnimarket/nodes/node_merge_sweep_state_reducer/handlers/handler_sweep_state.py:62",
        "omnimarket/nodes/node_merge_sweep_state_reducer/handlers/handler_sweep_state.py:66",
    ]
)


def _collect_reach_ins() -> list[tuple[str, str, str, str]]:
    """Return (key, importer_node, imported_node, line) for each reach-in found."""
    found: list[tuple[str, str, str, str]] = []
    nodes_root = _SRC_ROOT / "omnimarket" / "nodes"

    for py_file in nodes_root.rglob("*.py"):
        rel = py_file.relative_to(nodes_root)
        owning_node = rel.parts[0]

        for lineno, line in enumerate(
            py_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            m = _REACH_IN_PATTERN.search(line)
            if m is None:
                continue
            imported_node = m.group(1)
            if imported_node == owning_node:
                continue
            rel_from_src = py_file.relative_to(_SRC_ROOT)
            key = f"{rel_from_src}:{lineno}"
            found.append((key, owning_node, imported_node, line.strip()))

    return found


def test_no_new_cross_node_model_reach_ins() -> None:
    """No cross-node reach-ins outside the known pre-existing allowlist."""
    all_reach_ins = _collect_reach_ins()

    new_violations = [
        (key, importer, imported, code)
        for key, importer, imported, code in all_reach_ins
        if key not in _KNOWN_VIOLATIONS
    ]

    if not new_violations:
        return

    lines = [
        "New cross-node model reach-ins detected (move shared models to omnimarket.events.*):",
        "To add a temporary exception, add the key to _KNOWN_VIOLATIONS with a ticket reference.",
    ]
    for key, importer, imported, code in new_violations:
        lines.append(f"  {key}  [{importer}] → [{imported}]  |  {code}")

    pytest.fail("\n".join(lines))


def test_known_violations_not_grown() -> None:
    """The known-violations allowlist must not grow beyond its baseline count.

    This catches anyone silently expanding the allowlist without fixing the
    underlying reach-in. The count is the source of truth; update it only
    when violations are *fixed* (count decreases) — never when adding new ones.
    """
    baseline = 33
    assert len(_KNOWN_VIOLATIONS) <= baseline, (
        f"_KNOWN_VIOLATIONS grew from {baseline} to {len(_KNOWN_VIOLATIONS)}. "
        "Fix a reach-in to reduce it — do not add new entries."
    )


def test_ledger_reach_ins_fully_removed() -> None:
    """Ledger cross-node reach-ins (fixed by OMN-9263) must not reappear."""
    all_reach_ins = _collect_reach_ins()
    ledger_violations = [
        (key, code)
        for key, importer, imported, code in all_reach_ins
        if (
            "node_ledger" in importer
            and "node_ledger" in imported
            and importer != imported
            and (
                "model_ledger_appended_event" in code
                or "model_ledger_hash_computed" in code
            )
        )
    ]
    if ledger_violations:
        lines = ["Ledger cross-node reach-ins reintroduced (OMN-9263 regression):"]
        for key, code in ledger_violations:
            lines.append(f"  {key}  |  {code}")
        pytest.fail("\n".join(lines))
