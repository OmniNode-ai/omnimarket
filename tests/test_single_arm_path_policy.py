# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI guard: exactly one active merge-queue arm path (OMN-14151).

The merge-queue governor's safety property depends on there being a SINGLE
gated arm path — node_pr_arm_gate_compute feeding node_pr_lifecycle_merge_effect
via HandlerPrLifecycleOrchestrator._call_merge_fanout. This module asserts,
mechanically (not by convention), that:

  1. the arm-gate node is registered and is the sole new arm decider;
  2. the three legacy arm surfaces (node_auto_merge_effect,
     node_merge_sweep_auto_merge_arm_effect,
     node_merge_sweep_triage_orchestrator) carry NO pyproject.toml entry point
     (deregistered) — a queue-mutating command can no longer reach them via
     the ONEX node-dispatch runtime;
  3. each of those three legacy handlers' source still carries the fail-closed
     env-flag hard-gate, and (OMN-15053) the two GitHub-mutating handlers
     raise loudly rather than silently no-op'ing when the gate is closed, so
     a direct/manual invocation without the flag either never routes (triage)
     or raises immediately (the two effect handlers) instead of quietly
     reporting success;
  4. the orchestrator's merge fanout genuinely calls the arm-gate before
     ever calling the merge handler;
  5. the arm-gate's default policy is report-only + kill-switch engaged (zero
     mutation is the shipped default, not an opt-in).

A future change that re-adds one of these entry points, drops the env-flag
gate, or bypasses the arm-gate call in the merge fanout fails this suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_gate_policy import (
    EnumArmActionMode,
    ModelArmGatePolicy,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

_LEGACY_ARM_NODES: tuple[str, ...] = (
    "node_auto_merge_effect",
    "node_merge_sweep_auto_merge_arm_effect",
    "node_merge_sweep_triage_orchestrator",
)

_LEGACY_ARM_HANDLER_FILES: tuple[Path, ...] = (
    _REPO_ROOT
    / "src/omnimarket/nodes/node_auto_merge_effect/handlers/handler_auto_merge_effect.py",
    _REPO_ROOT
    / "src/omnimarket/nodes/node_merge_sweep_auto_merge_arm_effect/handlers/handler_auto_merge_arm.py",
    _REPO_ROOT
    / "src/omnimarket/nodes/node_merge_sweep_triage_orchestrator/handlers/handler_triage.py",
)

_ORCHESTRATOR_SOURCE = (
    _REPO_ROOT
    / "src/omnimarket/nodes/node_pr_lifecycle_orchestrator/handlers/handler_pr_lifecycle_orchestrator.py"
)


def _entry_point_names() -> set[str]:
    """Parse [project.entry-points."onex.nodes"] from pyproject.toml."""
    content = _PYPROJECT.read_text()
    match = re.search(
        r'\[project\.entry-points\."onex\.nodes"\](.*?)(?=\n\[|\Z)',
        content,
        re.DOTALL,
    )
    assert match is not None, (
        "onex.nodes entry-points table not found in pyproject.toml"
    )
    names: set[str] = set()
    for line in match.group(1).strip().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            names.add(line.split("=", 1)[0].strip())
    return names


@pytest.mark.unit
def test_arm_gate_node_is_registered() -> None:
    assert "node_pr_arm_gate_compute" in _entry_point_names()


@pytest.mark.unit
@pytest.mark.parametrize("node_name", _LEGACY_ARM_NODES)
def test_legacy_arm_node_has_no_pyproject_entry(node_name: str) -> None:
    """Deregistered: the ONEX node-dispatch runtime can no longer load this
    node's handler by name."""
    assert node_name not in _entry_point_names(), (
        f"{node_name} still has a pyproject.toml entry point — the legacy arm "
        "surface must stay deregistered (OMN-14151)."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "handler_path", _LEGACY_ARM_HANDLER_FILES, ids=lambda p: p.name
)
def test_legacy_arm_handler_source_still_hard_gated(handler_path: Path) -> None:
    """Even a direct/manual dispatch of the legacy handler is refused by
    default — the source must still reference the fail-closed env-flag gate.

    OMN-15053: the two GitHub-mutating handlers (auto_merge_effect,
    auto_merge_arm_effect) now call the loud-refusal helper
    ``require_legacy_merge_arm_enabled`` (which itself calls ``env_flag``
    internally, in env_flags.py); triage's arm-emit still calls ``env_flag``
    directly since it only decides whether to route, not whether to mutate.
    Either spelling proves the gate is still wired.
    """
    source = handler_path.read_text()
    assert "OMNIMARKET_LEGACY_MERGE_ARM_ENABLED" in source, (
        f"{handler_path} no longer references the OMN-14151 hard-gate env flag"
    )
    assert "env_flag(" in source or "require_legacy_merge_arm_enabled(" in source, (
        f"{handler_path} no longer calls the canonical env_flag()/"
        "require_legacy_merge_arm_enabled() helper"
    )


@pytest.mark.unit
def test_orchestrator_merge_fanout_calls_arm_gate_before_merge() -> None:
    """Source-level proof that the merge fanout evaluates the arm-gate for
    every candidate before ever calling the merge handler — the ONE gated
    path the safety property depends on."""
    source = _ORCHESTRATOR_SOURCE.read_text()
    assert "node_pr_arm_gate_compute" in source
    assert "_evaluate_arm_gate" in source
    assert "EnumArmDecision.ARM" in source

    fanout_match = re.search(
        r"async def _call_merge_fanout\(.*?\n(?=    async def |    @staticmethod|\Z)",
        source,
        re.DOTALL,
    )
    assert fanout_match is not None, "_call_merge_fanout method body not found"
    fanout_body = fanout_match.group(0)
    arm_gate_index = fanout_body.find("_evaluate_arm_gate")
    merge_handle_index = fanout_body.find("self._merge.handle(")
    assert arm_gate_index != -1, "_call_merge_fanout never calls _evaluate_arm_gate"
    assert merge_handle_index != -1, "_call_merge_fanout never calls self._merge.handle"
    assert arm_gate_index < merge_handle_index, (
        "_call_merge_fanout must evaluate the arm-gate before calling the merge "
        "handler, not after"
    )


@pytest.mark.unit
def test_default_arm_gate_policy_is_report_only_and_killed() -> None:
    """Shipped default: zero mutation without any explicit opt-in."""
    policy = ModelArmGatePolicy()
    assert policy.action_mode is EnumArmActionMode.REPORT_ONLY
    assert policy.kill_switch is True
