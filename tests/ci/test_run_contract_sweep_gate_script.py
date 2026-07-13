# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for scripts/ci/run_contract_sweep_gate.py CI/pre-commit gate
(OMN-14542, class fix, parent OMN-14531).

Drives the harness script directly (in-process, importing main()) against
synthetic --repo-root fixtures — proving the actual gate logic (independent
count cross-check + fail-closed scope invariant), not a copy of it.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

_VALID_CONTRACT = textwrap.dedent("""\
    name: node_test_valid
    node_type: COMPUTE_GENERIC
    contract_version:
      major: 1
      minor: 0
      patch: 0
    node_version: "1.0.0"
    description: "A valid test node"
    event_bus:
      publish_topics:
        - "onex.evt.platform.test-event.v1"
""")

_BAD_CONTRACT = "name: node_bad\nnode_type: compute\n"


def _write_contract(base: Path, node_name: str, content: str) -> Path:
    node_dir = base / "nodes" / node_name
    node_dir.mkdir(parents=True, exist_ok=True)
    contract = node_dir / "contract.yaml"
    contract.write_text(content)
    return contract


@pytest.mark.unit
class TestRunContractSweepGateScript:
    def test_exit_zero_on_healthy_populated_scope(self, tmp_path: Path) -> None:
        from run_contract_sweep_gate import main

        repo_root = tmp_path / "myrepo"
        for i in range(3):
            _write_contract(repo_root / "src", f"node_{i}", _VALID_CONTRACT)

        rc = main(["--repo-root", str(repo_root)])
        assert rc == 0

    def test_exit_nonzero_on_empty_scope(self, tmp_path: Path) -> None:
        """MANDATORY RED PROOF: an empty repo directory (exists, but zero
        contract.yaml anywhere under it) must fail the gate."""
        from run_contract_sweep_gate import main

        repo_root = tmp_path / "emptyrepo"
        repo_root.mkdir(parents=True)

        rc = main(["--repo-root", str(repo_root)])
        assert rc == 1

    def test_exit_zero_advisory_on_violations_without_strict(
        self, tmp_path: Path
    ) -> None:
        """Real violations do not block the gate unless --strict is passed
        (documented advisory-for-now decision — pre-existing debt tracked
        separately from the scope-invariant class fix)."""
        from run_contract_sweep_gate import main

        repo_root = tmp_path / "myrepo"
        _write_contract(repo_root / "src", "node_bad", _BAD_CONTRACT)

        rc = main(["--repo-root", str(repo_root)])
        assert rc == 0

    def test_exit_nonzero_on_violations_with_strict(self, tmp_path: Path) -> None:
        from run_contract_sweep_gate import main

        repo_root = tmp_path / "myrepo"
        _write_contract(repo_root / "src", "node_bad", _BAD_CONTRACT)

        rc = main(["--repo-root", str(repo_root), "--strict"])
        assert rc == 1

    def test_independent_probe_disagreement_is_blocking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the node's own scanned_count ever silently disagrees with the
        independent filesystem probe, the gate must refuse to pass —
        this is the literal "9 vs 941" defect made unrepresentable."""
        import run_contract_sweep_gate as gate_module

        repo_root = tmp_path / "myrepo"
        for i in range(3):
            _write_contract(repo_root / "src", f"node_{i}", _VALID_CONTRACT)

        # Force the independent probe to disagree with the node's real count.
        monkeypatch.setattr(
            gate_module, "_independent_contract_count", lambda _root: 999
        )

        rc = gate_module.main(["--repo-root", str(repo_root)])
        assert rc == 1
