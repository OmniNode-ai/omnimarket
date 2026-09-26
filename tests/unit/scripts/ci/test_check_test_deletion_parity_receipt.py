# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the OMN-19712 test-deletion parity-receipt gate."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Literal

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_GATE_PATH = _REPO_ROOT / "scripts/ci/check_test_deletion_parity_receipt.py"
_PARITY_PATH = _REPO_ROOT / "scripts/ci/chain_replacement_parity.py"
_TEST_PATH = Path("tests/planted/test_x.py")
_INPUTS_PATH = Path("validation/chain_replacement_receipts/planted.inputs.yaml")
_RECEIPT_PATH = Path("validation/chain_replacement_receipts/planted.json")
_CASE_ONE = "tests/planted/test_x.py::test_one"
_CASE_TWO = "tests/planted/test_x.py::TestGroup::test_two"
_TEST_SOURCE = """\
def test_one() -> None:
    assert True


class TestGroup:
    async def test_two(self) -> None:
        assert True
"""
_TEST_SOURCE_WITHOUT_ONE = """\
class TestGroup:
    async def test_two(self) -> None:
        assert True
"""

# Loading the gate by path does not add its directory to sys.path as executing
# the script does. The production import intentionally resolves its sibling
# chain_replacement_parity.py.
sys.path.insert(0, str(_GATE_PATH.parent))


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module("check_test_deletion_parity_receipt", _GATE_PATH)
parity = _load_module("chain_replacement_parity_for_deletion_gate_tests", _PARITY_PATH)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=scrub_git_location_env(os.environ),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(repo: Path, relative_path: Path, content: str) -> None:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def planted_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Parity Gate Test")
    _write(repo, _INPUTS_PATH, 'scope:\n  - "tests/planted/**"\n')
    _write(repo, _TEST_PATH, _TEST_SOURCE)
    return repo, _commit(repo, "baseline")


@pytest.fixture
def in_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)


def _run_gate(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
) -> int:
    monkeypatch.chdir(repo)
    return gate.main(list(args))


def _write_receipt(
    repo: Path,
    *,
    deleted_cases: tuple[str, ...],
    verdict: Literal["PASS", "FAIL"] = "PASS",
) -> None:
    checks = {
        check_id: parity.CheckResult(status="PASS", detail={})
        for check_id in ("P1", "P2", "P3", "P4", "P5")
    }
    receipt = parity.ParityReceipt(
        flow="planted",
        pinned_sha="0" * 40,
        handler_modules=("src/planted.py",),
        deleted_cases=deleted_cases,
        chain_cases=("tests/chains/test_planted.py::test_chain",),
        checks=checks,
        verdict=verdict,
    )
    _write(repo, _RECEIPT_PATH, receipt.model_dump_json(indent=2) + "\n")


def test_deleted_function_without_receipt_fails(
    planted_repo: tuple[Path, str],
    in_repo: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, base = planted_repo
    _write(repo, _TEST_PATH, _TEST_SOURCE_WITHOUT_ONE)
    _commit(repo, "delete test_one")

    assert _run_gate(repo, monkeypatch, "--changed-ref", base) == 1

    assert _CASE_ONE in capsys.readouterr().out


def test_deleted_function_with_pass_receipt_passes(
    planted_repo: tuple[Path, str],
    in_repo: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = planted_repo
    _write(repo, _TEST_PATH, _TEST_SOURCE_WITHOUT_ONE)
    _write_receipt(repo, deleted_cases=(_CASE_ONE,))
    _commit(repo, "replace test_one with a parity receipt")

    assert _run_gate(repo, monkeypatch, "--changed-ref", base) == 0


def test_fail_receipt_rejects_deleted_function(
    planted_repo: tuple[Path, str],
    in_repo: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, base = planted_repo
    _write(repo, _TEST_PATH, _TEST_SOURCE_WITHOUT_ONE)
    _write_receipt(repo, deleted_cases=(_CASE_ONE,), verdict="FAIL")
    _commit(repo, "delete test_one with failed parity")

    assert _run_gate(repo, monkeypatch, "--changed-ref", base) == 1

    output = capsys.readouterr().out
    assert _CASE_ONE in output
    assert "receipt verdict is FAIL" in output


def test_pass_receipt_must_admit_deleted_case(
    planted_repo: tuple[Path, str],
    in_repo: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, base = planted_repo
    _write(repo, _TEST_PATH, _TEST_SOURCE_WITHOUT_ONE)
    _write_receipt(repo, deleted_cases=(_CASE_TWO,))
    _commit(repo, "delete unadmitted test_one")

    assert _run_gate(repo, monkeypatch, "--changed-ref", base) == 1

    output = capsys.readouterr().out
    assert _CASE_ONE in output
    assert "case not admitted" in output


def test_whole_file_deletion_checks_every_case(
    planted_repo: tuple[Path, str],
    in_repo: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, base = planted_repo
    (repo / _TEST_PATH).unlink()
    _write_receipt(repo, deleted_cases=(_CASE_ONE,))
    _commit(repo, "delete test file with partial receipt")

    assert _run_gate(repo, monkeypatch, "--changed-ref", base) == 1

    output = capsys.readouterr().out
    assert _CASE_TWO in output
    assert "case not admitted" in output


def test_deletion_outside_scope_is_explicitly_skipped(
    planted_repo: tuple[Path, str],
    in_repo: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, _ = planted_repo
    _write(repo, _INPUTS_PATH, 'scope:\n  - "tests/other/**"\n')
    base = _commit(repo, "move planted flow scope")
    _write(repo, _TEST_PATH, _TEST_SOURCE_WITHOUT_ONE)
    _commit(repo, "delete out-of-scope test_one")

    assert _run_gate(repo, monkeypatch, "--changed-ref", base) == 0
    assert f"[SKIP] {_CASE_ONE}: not in any chain-replacement flow scope" in (
        capsys.readouterr().out
    )

    assert _run_gate(repo, monkeypatch, "--changed-ref", base, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["skipped"] == 1


def test_removing_flow_declaration_does_not_remove_base_scope(
    planted_repo: tuple[Path, str],
    in_repo: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, base = planted_repo
    (repo / _INPUTS_PATH).unlink()
    _write(repo, _TEST_PATH, _TEST_SOURCE_WITHOUT_ONE)
    _commit(repo, "remove flow declaration and test_one")

    assert _run_gate(repo, monkeypatch, "--changed-ref", base) == 1

    output = capsys.readouterr().out
    assert _CASE_ONE in output
    assert "planted" in output


def test_malformed_inputs_fails_and_names_file(
    planted_repo: tuple[Path, str],
    in_repo: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, base = planted_repo
    _write(repo, _INPUTS_PATH, "scope: tests/planted/**\n")
    _write(repo, _TEST_PATH, _TEST_SOURCE_WITHOUT_ONE)
    _commit(repo, "malform flow declaration and delete test_one")

    assert _run_gate(repo, monkeypatch, "--changed-ref", base) == 1

    assert _INPUTS_PATH.as_posix() in capsys.readouterr().out


def test_rename_without_removed_case_passes(
    planted_repo: tuple[Path, str],
    in_repo: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = planted_repo
    _git(repo, "mv", _TEST_PATH.as_posix(), "tests/planted/test_y.py")
    _commit(repo, "rename test file")

    assert _run_gate(repo, monkeypatch, "--changed-ref", base) == 0


def test_staged_mode_detects_staged_deletion(
    planted_repo: tuple[Path, str],
    in_repo: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, _ = planted_repo
    _write(repo, _TEST_PATH, _TEST_SOURCE_WITHOUT_ONE)
    _git(repo, "add", _TEST_PATH.as_posix())

    assert _run_gate(repo, monkeypatch, "--staged") == 1

    assert _CASE_ONE in capsys.readouterr().out
