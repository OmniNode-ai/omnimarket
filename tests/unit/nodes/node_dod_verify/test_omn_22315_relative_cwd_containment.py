# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-22315: a relative ``cwd`` names the contract's own tree, not the process cwd.

A check declaring ``cwd: "."`` was resolved against the verifier's process cwd
and then required to sit under ``OMNI_HOME``. The same check read verified when
the verifier was started inside a clone under ``OMNI_HOME`` and failed with
"cwd escapes OMNI_HOME containment" when it was started anywhere else: a lane
clone outside the registry root, or the evidence autoclose sweep, whose dispatch
cwd is a contract-roots clone on the runner. A check that omits ``cwd`` already
runs in the contract's own tree with no containment test.

These tests pin the new rule and the refusals that must survive it. The
containment tests are the guard against weakening the gate: an absolute path
outside the root, a ``..`` traversal and a symlink that leaves both roots stay
refused, and a command that fails still fails.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

pytestmark = pytest.mark.unit


def _repo_with_contract(
    root: Path, command: str, cwd: str, name: str = "product"
) -> tuple[Path, Path]:
    """A repository tree holding ``contracts/OMN-TEST.yaml`` with one command check."""
    tree = root / name
    contracts = tree / "contracts"
    contracts.mkdir(parents=True)
    contract = {
        "schema_version": "1.0.0",
        "ticket_id": "OMN-TEST",
        "dod_evidence": [
            {
                "id": "dod-001",
                "description": "relative cwd",
                "checks": [
                    {"check_type": "command", "command": command, "cwd": cwd},
                ],
            }
        ],
    }
    path = contracts / "OMN-TEST.yaml"
    path.write_text(yaml.dump(contract), encoding="utf-8")
    return tree, path


@pytest.fixture
def outside_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """OMNI_HOME in one directory, the verifier process started in another."""
    home = tmp_path / "registry"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("OMNI_HOME", str(home))
    monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)
    monkeypatch.chdir(elsewhere)
    return home, elsewhere


def _run(contract: Path) -> tuple[EnumEvidenceCheckStatus, str]:
    results = EvidenceCollector().collect("OMN-TEST", contract_path=str(contract))
    return results[0].status, results[0].message or ""


def test_relative_cwd_outside_omni_home_verifies(
    tmp_path: Path, outside_process_cwd: tuple[Path, Path]
) -> None:
    """AC1: ``cwd: "."`` runs in the contract's tree when the process cwd is outside OMNI_HOME."""
    tree, contract = _repo_with_contract(tmp_path / "lane", "pwd", ".")
    status, message = _run(contract)
    assert status == EnumEvidenceCheckStatus.VERIFIED, message
    assert str(tree.resolve()) in message


def test_relative_subdirectory_cwd_outside_omni_home_verifies(
    tmp_path: Path, outside_process_cwd: tuple[Path, Path]
) -> None:
    """AC1: a relative subdirectory is resolved under the contract's tree too."""
    tree, contract = _repo_with_contract(tmp_path / "lane", "pwd", "src")
    (tree / "src").mkdir()
    status, message = _run(contract)
    assert status == EnumEvidenceCheckStatus.VERIFIED, message
    assert str((tree / "src").resolve()) in message


def test_relative_cwd_runs_in_contract_tree_not_process_cwd(
    tmp_path: Path, outside_process_cwd: tuple[Path, Path]
) -> None:
    """The verdict no longer depends on where the verifier was started."""
    _, elsewhere = outside_process_cwd
    tree, contract = _repo_with_contract(tmp_path / "lane", "test -f marker", ".")
    (tree / "marker").write_text("x", encoding="utf-8")
    (elsewhere / "other").write_text("x", encoding="utf-8")
    status, message = _run(contract)
    assert status == EnumEvidenceCheckStatus.VERIFIED, message


def test_relative_cwd_failing_command_fails(
    tmp_path: Path, outside_process_cwd: tuple[Path, Path]
) -> None:
    """AC3: a command that fails under a relative cwd is FAILED, never verified."""
    _, contract = _repo_with_contract(tmp_path / "lane", "false", ".")
    status, message = _run(contract)
    assert status == EnumEvidenceCheckStatus.FAILED
    assert "escapes" not in message


def test_relative_cwd_missing_file_check_fails(
    tmp_path: Path, outside_process_cwd: tuple[Path, Path]
) -> None:
    """AC3: evidence that is absent from the contract's tree reads FAILED."""
    _, contract = _repo_with_contract(tmp_path / "lane", "test -f absent-marker", ".")
    status, _ = _run(contract)
    assert status == EnumEvidenceCheckStatus.FAILED


def test_relative_cwd_still_contained_absolute_outside_refused(
    tmp_path: Path, outside_process_cwd: tuple[Path, Path]
) -> None:
    """AC2: an absolute cwd outside OMNI_HOME stays refused, even beside the contract."""
    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    _, contract = _repo_with_contract(tmp_path / "lane", "pwd", str(outside))
    status, message = _run(contract)
    assert status == EnumEvidenceCheckStatus.FAILED
    assert "escapes OMNI_HOME containment" in message


def test_relative_cwd_still_contained_absolute_own_tree_refused(
    tmp_path: Path, outside_process_cwd: tuple[Path, Path]
) -> None:
    """AC2: the own-tree allowance is for relative cwds only."""
    tree, contract = _repo_with_contract(tmp_path / "lane", "pwd", "placeholder")
    spec = yaml.safe_load(contract.read_text(encoding="utf-8"))
    spec["dod_evidence"][0]["checks"][0]["cwd"] = str(tree)
    contract.write_text(yaml.dump(spec), encoding="utf-8")
    status, message = _run(contract)
    assert status == EnumEvidenceCheckStatus.FAILED
    assert "escapes OMNI_HOME containment" in message


def test_relative_cwd_still_contained_traversal_refused(
    tmp_path: Path, outside_process_cwd: tuple[Path, Path]
) -> None:
    """AC2: ``..`` is refused before any anchoring."""
    _, contract = _repo_with_contract(tmp_path / "lane", "pwd", "../sibling")
    (tmp_path / "lane" / "sibling").mkdir()
    status, message = _run(contract)
    assert status == EnumEvidenceCheckStatus.FAILED
    assert "traversal" in message.lower()


def test_relative_cwd_still_contained_symlink_leaving_both_roots_refused(
    tmp_path: Path, outside_process_cwd: tuple[Path, Path]
) -> None:
    """AC2: a symlink in the contract's tree that leaves the tree and OMNI_HOME is refused."""
    outside = tmp_path / "far-away"
    outside.mkdir()
    tree, contract = _repo_with_contract(tmp_path / "lane", "pwd", "escape")
    os.symlink(outside, tree / "escape")
    status, message = _run(contract)
    assert status == EnumEvidenceCheckStatus.FAILED
    assert "escapes OMNI_HOME containment" in message


def test_relative_cwd_without_a_contract_tree_keeps_process_cwd_rule(
    tmp_path: Path, outside_process_cwd: tuple[Path, Path]
) -> None:
    """A contract outside any ``contracts/`` directory has no own tree to anchor at."""
    spec = {
        "schema_version": "1.0.0",
        "ticket_id": "OMN-TEST",
        "dod_evidence": [
            {
                "id": "dod-001",
                "description": "loose contract",
                "checks": [{"check_type": "command", "command": "pwd", "cwd": "."}],
            }
        ],
    }
    loose = tmp_path / "loose"
    loose.mkdir()
    contract = loose / "OMN-TEST.yaml"
    contract.write_text(yaml.dump(spec), encoding="utf-8")
    status, message = _run(contract)
    assert status == EnumEvidenceCheckStatus.FAILED
    assert "escapes OMNI_HOME containment" in message


def test_relative_cwd_inside_omni_home_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verifier started under OMNI_HOME behaves as before."""
    home = tmp_path / "registry"
    tree, contract = _repo_with_contract(home, "pwd", ".")
    monkeypatch.setenv("OMNI_HOME", str(home))
    monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)
    monkeypatch.chdir(tree)
    status, message = _run(contract)
    assert status == EnumEvidenceCheckStatus.VERIFIED, message
