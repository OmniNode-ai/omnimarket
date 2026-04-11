#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Proof-of-life script for node_overseer_verifier.

Steps:
1. PASS case   — valid envelope, all checks green.
2. FAIL case   — negative cost_so_far triggers invariant_preservation failure.
3. ESCALATE case — low confidence triggers ESCALATE verdict.
4. FAIL (action scope) — unknown action triggers FAIL with CONFIGURATION class.

Related:
    - OMN-8035: Proof of life — run verifier and seam-parallel via onex run
"""

from __future__ import annotations

import json
import sys

from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier import (
    HandlerOverseerVerifier,
)
from omnimarket.nodes.node_overseer_verifier.models.model_verifier_request import (
    ModelVerifierRequest,
)


def _print_result(label: str, result: dict[str, object]) -> None:
    print(f"\n{'=' * 60}")
    print(f"CASE: {label}")
    print(f"{'=' * 60}")
    print(json.dumps(result, indent=2, default=str))


def main() -> int:
    handler = HandlerOverseerVerifier()

    # ------------------------------------------------------------------
    # Case 1: PASS — minimal valid envelope
    # ------------------------------------------------------------------
    request_pass = ModelVerifierRequest(
        task_id="task-001",
        status="completed",
        domain="overseer",
        node_id="node_overseer_verifier",
        confidence=0.92,
        cost_so_far=0.0042,
        allowed_actions=["dispatch", "complete"],
        declared_invariants=["cost_non_negative"],
        schema_version="1.0",
    )
    result_pass = handler.verify(request_pass)
    _print_result("PASS — valid envelope", result_pass)

    assert result_pass["verdict"] == "PASS", (
        f"Expected PASS, got {result_pass['verdict']!r}"
    )
    assert result_pass["failure_class"] is None
    print("ASSERTION: verdict == 'PASS'  ✓")
    print("ASSERTION: failure_class is None  ✓")

    # ------------------------------------------------------------------
    # Case 2: ESCALATE — low confidence triggers VERIFIER_REJECTION
    # ------------------------------------------------------------------
    request_low_conf = ModelVerifierRequest(
        task_id="task-002",
        status="completed",
        domain="overseer",
        node_id="node_overseer_verifier",
        confidence=0.12,
        cost_so_far=0.001,
        allowed_actions=["complete"],
        schema_version="1.0",
    )
    result_escalate = handler.verify(request_low_conf)
    _print_result("ESCALATE — low confidence (0.12)", result_escalate)

    assert result_escalate["verdict"] == "ESCALATE", (
        f"Expected ESCALATE, got {result_escalate['verdict']!r}"
    )
    assert result_escalate["failure_class"] == "PERMANENT", (
        f"Expected failure_class == 'PERMANENT', got {result_escalate['failure_class']!r}"
    )
    print("ASSERTION: verdict == 'ESCALATE'  ✓")
    print(f"ASSERTION: failure_class == {result_escalate['failure_class']!r}  ✓")

    # ------------------------------------------------------------------
    # Case 3: ESCALATE — negative cost triggers invariant_preservation failure
    # ------------------------------------------------------------------
    request_neg_cost = ModelVerifierRequest(
        task_id="task-003",
        status="completed",
        domain="overseer",
        node_id="node_overseer_verifier",
        confidence=0.95,
        cost_so_far=-0.5,
        allowed_actions=["complete"],
        schema_version="1.0",
    )
    result_fail = handler.verify(request_neg_cost)
    _print_result("ESCALATE — negative cost_so_far", result_fail)

    assert result_fail["verdict"] == "ESCALATE", (
        f"Expected ESCALATE, got {result_fail['verdict']!r}"
    )
    assert result_fail["failure_class"] == "DATA_INTEGRITY", (
        f"Expected failure_class == 'DATA_INTEGRITY', got {result_fail['failure_class']!r}"
    )
    print("ASSERTION: verdict == 'ESCALATE'  ✓")
    print(f"ASSERTION: failure_class == {result_fail['failure_class']!r}  ✓")

    # ------------------------------------------------------------------
    # Case 4: FAIL — unknown action triggers allowed_action_scope failure
    # ------------------------------------------------------------------
    request_bad_action = ModelVerifierRequest(
        task_id="task-004",
        status="running",
        domain="overseer",
        node_id="node_overseer_verifier",
        confidence=0.88,
        cost_so_far=0.002,
        allowed_actions=["dispatch", "delete_all"],
        schema_version="1.0",
    )
    result_bad_action = handler.verify(request_bad_action)
    _print_result("FAIL — unknown action 'delete_all'", result_bad_action)

    assert result_bad_action["verdict"] == "FAIL", (
        f"Expected FAIL, got {result_bad_action['verdict']!r}"
    )
    assert result_bad_action["failure_class"] == "CONFIGURATION"
    print("ASSERTION: verdict == 'FAIL'  ✓")
    print("ASSERTION: failure_class == 'CONFIGURATION'  ✓")

    print("\n" + "=" * 60)
    print("ALL VERIFIER PROOF-OF-LIFE ASSERTIONS PASSED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
