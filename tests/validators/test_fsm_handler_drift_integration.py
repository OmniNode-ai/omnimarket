# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Integration tests for FSM handler drift validator against omnimarket (OMN-13735).

Three mandatory TDD cases:
  1. node_intelligence_reducer: zero violations (positive case — real binding).
  2. Injected divergence on a synthetic fixture asserts ValidatorViolation
     (ModelFsmHandlerDriftFinding with non-zero count).
  3. Hook script exits non-zero on divergence fixture, zero on aligned fixture.

Pre-condition verified here: grep output enumerated in test_precondition_no_drift_found.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# OMN-13735: the validator ships in omnibase_core. omnimarket's pinned core
# (tool.uv.sources rev) predates it until the post-merge pin bump, so skip
# cleanly in the base suite when the module is absent. The dedicated
# validator-fsm-handler-drift.yml workflow installs core from source, so this
# test runs (and gates) there — and locally once the pin bump lands.
pytest.importorskip(
    "omnibase_core.validators.fsm_handler_drift",
    reason="requires the OMN-13735 FSM drift validator from omnibase_core "
    "(present after the core pin bump / in the dedicated CI gate)",
)

from omnibase_core.enums.enum_fsm_handler_drift import EnumFsmHandlerDriftKind
from omnibase_core.validators.fsm_handler_drift import (
    validate_contract,
    validate_root,
)

pytestmark = pytest.mark.unit

# Repo root of omnimarket (resolved relative to this test file).
_OMNIMARKET_ROOT = Path(__file__).parent.parent.parent


# --------------------------------------------------------------------------- #
# TDD Case 1 — Positive: node_intelligence_reducer has zero violations
# --------------------------------------------------------------------------- #


def test_node_intelligence_reducer_zero_violations() -> None:
    """TDD case 1: node_intelligence_reducer contract + handler pass with zero findings.

    Confirms the PATTERN_LIFECYCLE fsm_handler_binding declared in
    node_intelligence_reducer/contract.yaml matches handler_pattern_lifecycle.py
    exactly — no drift at time of landing.
    """
    contract_path = (
        _OMNIMARKET_ROOT
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_intelligence_reducer"
        / "contract.yaml"
    )
    assert contract_path.is_file(), (
        f"contract.yaml not found at {contract_path}; "
        "ensure the omnimarket worktree is set up correctly"
    )

    findings = validate_contract(contract_path, scan_root=_OMNIMARKET_ROOT)
    assert findings == [], (
        f"Expected zero drift findings for node_intelligence_reducer, got "
        f"{len(findings)}:\n" + "\n".join(f.format() for f in findings)
    )


def test_node_intelligence_reducer_zero_violations_via_root_scan() -> None:
    """TDD case 1b: full root scan of omnimarket produces zero drift findings."""
    findings = validate_root(_OMNIMARKET_ROOT)
    assert findings == [], (
        f"Expected zero drift findings in full omnimarket scan, got "
        f"{len(findings)}:\n" + "\n".join(f.format() for f in findings)
    )


# --------------------------------------------------------------------------- #
# TDD Case 2 — Negative: injected divergence raises findings
# --------------------------------------------------------------------------- #


def _make_synthetic_node(
    root: Path,
    *,
    node: str,
    contract_yaml: str,
    handler_py: str | None = None,
    pkg: str = "synth",
) -> tuple[Path, Path | None]:
    node_dir = root / "src" / pkg / "nodes" / node
    node_dir.mkdir(parents=True, exist_ok=True)
    contract_path = node_dir / "contract.yaml"
    contract_path.write_text(textwrap.dedent(contract_yaml), encoding="utf-8")
    handler_path: Path | None = None
    if handler_py is not None:
        handler_dir = node_dir / "handlers"
        handler_dir.mkdir(exist_ok=True)
        handler_full = handler_dir / "handler_fsm.py"
        handler_full.write_text(textwrap.dedent(handler_py), encoding="utf-8")
        handler_path = handler_full
    return contract_path, handler_path


_SYNTHETIC_CONTRACT = """\
    state_machine:
      state_machine_name: "synth_fsm"
      initial_state: "open"
      states:
        - state_name: "open"
        - state_name: "closed"
        - state_name: "archived"
      transitions:
        - from_state: "open"
          to_state: "closed"
          trigger: "close"
        - from_state: "closed"
          to_state: "archived"
          trigger: "archive"
          guard_conditions:
            - field: "actor_role"
              operator: "eq"
              value: "admin"
              error_message: "archive requires admin"
    fsm_handler_binding:
      - fsm_type: "SYNTH_FSM"
        handler_module: "synth.nodes.node_synth.handlers.handler_fsm"
        valid_transitions_symbol: "VALID_TRANSITIONS"
        guard_conditions_symbol: "GUARD_CONDITIONS"
        state_filter:
          - "open"
          - "closed"
          - "archived"
"""

_ALIGNED_HANDLER = """\
    from typing import Final
    VALID_TRANSITIONS: Final[dict[tuple[str, str], str]] = {
        ("open", "close"): "closed",
        ("closed", "archive"): "archived",
    }
    GUARD_CONDITIONS: Final[dict[tuple[str, str], tuple[str, str, str]]] = {
        ("closed", "archive"): ("actor_role", "admin", "archive requires admin"),
    }
"""

_DIVERGED_HANDLER = """\
    from typing import Final
    # INJECTED DRIFT: removed ("closed", "archive") -> "archived"
    # and changed ("open", "close") -> "suspended" (wrong to_state)
    VALID_TRANSITIONS: Final[dict[tuple[str, str], str]] = {
        ("open", "close"): "suspended",
    }
    GUARD_CONDITIONS: Final[dict[tuple[str, str], tuple[str, str, str]]] = {}
"""


def test_injected_divergence_raises_violation(tmp_path: Path) -> None:
    """TDD case 2: ModelFsmHandlerDriftFinding is raised on injected divergence.

    This is the negative test that confirms the validator mechanically fires
    on a deliberate YAML-vs-handler divergence.
    """
    _make_synthetic_node(
        tmp_path,
        node="node_synth",
        contract_yaml=_SYNTHETIC_CONTRACT,
        handler_py=_DIVERGED_HANDLER,
    )
    findings = validate_root(tmp_path)
    assert len(findings) >= 1, (
        f"Expected at least 1 finding for injected drift, got {len(findings)}"
    )
    kinds = {f.kind for f in findings}
    # Must find transition mismatch and/or missing transition
    assert (
        EnumFsmHandlerDriftKind.TRANSITION_MISSING_IN_HANDLER in kinds
        or EnumFsmHandlerDriftKind.TRANSITION_TO_STATE_MISMATCH in kinds
    ), f"Expected drift findings, got kinds: {kinds}"


def test_aligned_synthetic_zero_findings(tmp_path: Path) -> None:
    """TDD case 2b: aligned synthetic handler produces zero findings."""
    _make_synthetic_node(
        tmp_path,
        node="node_synth",
        contract_yaml=_SYNTHETIC_CONTRACT,
        handler_py=_ALIGNED_HANDLER,
    )
    findings = validate_root(tmp_path)
    assert findings == [], "\n".join(f.format() for f in findings)


# --------------------------------------------------------------------------- #
# TDD Case 3 — Hook script exit codes
# --------------------------------------------------------------------------- #


def test_hook_script_nonzero_on_divergence(tmp_path: Path) -> None:
    """TDD case 3a: hook script exits non-zero on divergence fixture."""
    _make_synthetic_node(
        tmp_path,
        node="node_synth",
        contract_yaml=_SYNTHETIC_CONTRACT,
        handler_py=_DIVERGED_HANDLER,
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnibase_core.validators.fsm_handler_drift",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, (
        f"Expected exit code 1 (divergence), got {result.returncode}\n"
        f"stderr: {result.stderr}"
    )
    assert "ERROR" in result.stderr, f"Expected ERROR in stderr, got: {result.stderr}"


def test_hook_script_zero_on_aligned(tmp_path: Path) -> None:
    """TDD case 3b: hook script exits zero on aligned fixture."""
    _make_synthetic_node(
        tmp_path,
        node="node_synth",
        contract_yaml=_SYNTHETIC_CONTRACT,
        handler_py=_ALIGNED_HANDLER,
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnibase_core.validators.fsm_handler_drift",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"Expected exit code 0 (aligned), got {result.returncode}\n"
        f"stderr: {result.stderr}"
    )


# --------------------------------------------------------------------------- #
# Pre-condition verification: enumerate VALID_TRANSITIONS/GUARD_CONDITIONS
# --------------------------------------------------------------------------- #


def test_precondition_no_drift_found() -> None:
    """Pre-condition: verify no drift exists in omnimarket at time of landing.

    Enumerates all files with VALID_TRANSITIONS or GUARD_CONDITIONS in src/
    and confirms the validator finds zero drift. This is the structural proof
    required by OMN-13735 before the guard can land.

    Pre-condition grep output:
      $ grep -r 'VALID_TRANSITIONS|GUARD_CONDITIONS' omnimarket/src/ --include='*.py' -l
      src/omnimarket/nodes/node_intelligence_reducer/handlers/handler_pattern_lifecycle.py
        -> Has VALID_TRANSITIONS + GUARD_CONDITIONS; covered by fsm_handler_binding
      src/omnimarket/nodes/node_intelligence_reducer/handlers/__init__.py
        -> Re-exports from handler_pattern_lifecycle (no independent table)
      src/omnimarket/nodes/node_intelligence_reducer/node_tests/test_handler_pattern_lifecycle.py
        -> Test file; uses imported symbols (no independent table)
      src/omnimarket/nodes/node_intelligence_reducer/node_tests/test_node_pattern_lifecycle_integration.py
        -> Test file; uses imported symbols (no independent table)
      src/omnimarket/nodes/node_memory_lifecycle_orchestrator/validators/validator_lifecycle_transition.py
        -> Different FSM pattern (state->frozenset[state], no trigger); out of scope
      src/omnimarket/nodes/node_memory_lifecycle_orchestrator/validators/__init__.py
        -> Re-exports from validator_lifecycle_transition; out of scope
      src/omnimarket/nodes/node_delegation_orchestrator/handlers/handler_delegation_workflow.py
        -> Only in comments; the VALID_TRANSITIONS table was removed (confirmed by grep)

    Result: exactly one fsm_handler_binding declared (PATTERN_LIFECYCLE in
    node_intelligence_reducer), zero drift confirmed by validate_root() above.
    """
    findings = validate_root(_OMNIMARKET_ROOT)
    # Enumerated files with VALID_TRANSITIONS/GUARD_CONDITIONS — see docstring above.
    # Only handler_pattern_lifecycle.py has an active (from_state, trigger)->to_state
    # table matching the drift guard's expected pattern. It is aligned with the contract.
    assert findings == [], (
        "Pre-condition FAILED: existing drift found in omnimarket before guard lands:\n"
        + "\n".join(f.format() for f in findings)
    )
