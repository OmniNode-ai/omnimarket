# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for NodeContractSweep handler.

Covers:
- .venv / site-packages paths are excluded from scanning (OMN-9445)
- Valid topic names pass without violation
- Invalid topic names (wrong kind segment) produce INVALID_TOPIC_NAME violations
- Missing required fields produce MISSING_REQUIRED_FIELD violations
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from omnimarket.nodes.node_contract_sweep.handlers.handler_contract_sweep import (
    ContractSweepRequest,
    EnumViolationType,
    NodeContractSweep,
)


def _write_contract(base: Path, node_name: str, content: str) -> Path:
    """Write a contract.yaml under base/nodes/<node_name>/contract.yaml."""
    node_dir = base / "nodes" / node_name
    node_dir.mkdir(parents=True, exist_ok=True)
    contract = node_dir / "contract.yaml"
    contract.write_text(content)
    return contract


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
      subscribe_topics:
        - "onex.cmd.platform.test-cmd.v1"
""")

_SNAPSHOT_KIND_CONTRACT = textwrap.dedent("""\
    name: node_test_snapshot
    node_type: ORCHESTRATOR_GENERIC
    contract_version:
      major: 1
      minor: 0
      patch: 0
    node_version: "1.0.0"
    description: "Node with snapshot-kind topic (old convention, invalid)"
    event_bus:
      publish_topics:
        - "onex.snapshot.platform.registration-snapshots.v1"
""")


@pytest.mark.unit
class TestHandlerContractSweepVenvExclusion:
    """OMN-9445: .venv and site-packages paths must be excluded from scanning."""

    def test_venv_path_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contract inside .venv is not scanned even if it has violations."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        # Repo with a valid src structure so it's picked up by repo discovery
        repo = tmp_path / "some_repo"
        (repo / "src").mkdir(parents=True)

        # Plant a BAD contract inside .venv — should be skipped
        venv_node = repo / ".venv" / "lib" / "python3.12" / "site-packages"
        _write_contract(venv_node, "node_bad_venv", _SNAPSHOT_KIND_CONTRACT)

        result = NodeContractSweep().handle(ContractSweepRequest())

        assert result.contracts_checked == 0
        assert result.violations == []

    def test_site_packages_path_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contract directly under site-packages is not scanned."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        repo = tmp_path / "some_repo"
        (repo / "src").mkdir(parents=True)

        # site-packages without .venv prefix (edge case)
        pkg_dir = repo / "lib" / "site-packages"
        _write_contract(pkg_dir, "node_bad_pkg", _SNAPSHOT_KIND_CONTRACT)

        result = NodeContractSweep().handle(ContractSweepRequest())

        assert result.contracts_checked == 0
        assert result.violations == []

    def test_source_contract_is_scanned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contract under src/ (not .venv) is scanned normally."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        repo = tmp_path / "some_repo"
        src = repo / "src"
        src.mkdir(parents=True)
        _write_contract(src, "node_valid_src", _VALID_CONTRACT)

        result = NodeContractSweep().handle(ContractSweepRequest())

        assert result.contracts_checked == 1
        assert result.violations == []

    def test_venv_skipped_while_src_scanned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When both .venv and src/ contracts exist, only src/ is counted."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        repo = tmp_path / "some_repo"
        src = repo / "src"
        src.mkdir(parents=True)

        # Good contract in src/
        _write_contract(src, "node_valid_src", _VALID_CONTRACT)

        # Bad contract in .venv — should be ignored
        venv_node = repo / ".venv" / "lib" / "python3.12" / "site-packages"
        _write_contract(venv_node, "node_bad_venv", _SNAPSHOT_KIND_CONTRACT)

        result = NodeContractSweep().handle(ContractSweepRequest())

        assert result.contracts_checked == 1
        assert result.violations == []


@pytest.mark.unit
class TestHandlerContractSweepTopicValidation:
    """Topic naming validation covers cmd|evt|intent kinds only."""

    def test_valid_evt_topic_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        _write_contract(repo / "src", "node_valid", _VALID_CONTRACT)

        result = NodeContractSweep().handle(ContractSweepRequest())
        assert result.violations == []

    def test_snapshot_kind_topic_produces_violation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        _write_contract(repo / "src", "node_snapshot", _SNAPSHOT_KIND_CONTRACT)

        result = NodeContractSweep().handle(ContractSweepRequest())
        assert result.contracts_checked == 1
        topic_violations = [
            v
            for v in result.violations
            if v.violation_type == EnumViolationType.INVALID_TOPIC_NAME
        ]
        assert len(topic_violations) == 1
        assert (
            "onex.snapshot.platform.registration-snapshots.v1"
            in topic_violations[0].message
        )

    def test_missing_required_fields_produces_violations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        _write_contract(
            repo / "src",
            "node_incomplete",
            "name: node_incomplete\n",  # Missing all other required fields
        )

        result = NodeContractSweep().handle(ContractSweepRequest())
        assert result.contracts_checked == 1
        missing = [
            v
            for v in result.violations
            if v.violation_type == EnumViolationType.MISSING_REQUIRED_FIELD
        ]
        missing_names = {v.field for v in missing}
        assert "contract_version" in missing_names
        assert "node_type" in missing_names
        assert "node_version" in missing_names
        assert "description" in missing_names
