# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the pre-commit interpreter-resolution gate (OMN-17219).

Recurrence guard for the defect class reported by an external collaborator: a
pre-commit hook whose ``entry:`` shells out to a bare ``python`` refuses every
``git commit`` on macOS outside an activated venv ("Executable `python` not
found"), because macOS ships ``python3`` only. Same class as OMN-16958
(omnimemory's ``validate-spdx-headers``).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
GATE = REPO_ROOT / "scripts" / "validation" / "validate_precommit_interpreter.py"
CONFIG = REPO_ROOT / ".pre-commit-config.yaml"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("precommit_interpreter_gate", GATE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate() -> Any:
    return _load()


@pytest.mark.unit
def test_gate_script_exists_and_is_executable() -> None:
    assert GATE.is_file()


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry",
    [
        "python scripts/ci/check_one_occ_producer.py",
        "python3 scripts/ci/check_one_occ_producer.py",
        "env PYTHONPATH=src python -m omnimarket.validators.x",
        "exec python scripts/ci/x.py",
        "bash -c 'python scripts/ci/x.py'",
    ],
)
def test_bare_interpreter_entry_is_rejected(gate: Any, entry: str) -> None:
    violations = gate._scan_entry("some-hook", entry)
    assert violations, f"expected a violation for {entry!r}"
    assert "bare" in violations[0]


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry",
    [
        "uv run python scripts/ci/x.py",
        "uv run --frozen python scripts/check_dep_provenance.py",
        "uv run --no-project --with pyyaml python scripts/ci/x.py",
        "env PYTHONPATH=src uv run python -m omnimarket.validators.x",
        "bash -c 'uv run mypy src/omnimarket'",
        "/opt/homebrew/bin/python3 scripts/ci/x.py",
        '"$PYTHON" scripts/ci/x.py',
        "uv run pytest tests/ci/test_x.py -q",
        ".pre-commit-hooks/reject-deploy-gate-skip-token.sh",
    ],
)
def test_resolvable_interpreter_entry_is_accepted(gate: Any, entry: str) -> None:
    assert gate._scan_entry("some-hook", entry) == []


@pytest.mark.unit
def test_suppression_marker_is_honored(gate: Any) -> None:
    entry = "python scripts/ci/x.py  # precommit-interp-ok: illustrative"
    assert gate._scan_entry("some-hook", entry) == []


@pytest.mark.unit
def test_shell_script_bare_python_is_rejected(gate: Any, tmp_path: Path) -> None:
    script = tmp_path / "hook.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "# python foo.py  <- a comment, not an invocation\n"
        "command -v python3 >/dev/null && python3 -c 'import yaml'\n"
        "python scripts/ci/x.py\n"
    )
    monkey_root = gate.REPO_ROOT
    gate.REPO_ROOT = tmp_path
    try:
        violations = gate._scan_script(script)
    finally:
        gate.REPO_ROOT = monkey_root
    assert len(violations) == 1
    assert violations[0].startswith("hook.sh:4:")


@pytest.mark.unit
def test_live_config_has_no_bare_interpreter_hooks() -> None:
    """The repo's own committed config must satisfy the rule (RED before fix)."""
    result = subprocess.run(
        [sys.executable, str(GATE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "committed .pre-commit-config.yaml has a bare-interpreter hook:\n"
        f"{result.stdout}\n{result.stderr}"
    )


@pytest.mark.unit
def test_gate_fails_closed_on_vacuous_scan(gate: Any, tmp_path: Path) -> None:
    """A parse regression that scans nothing must fail, not silently pass."""
    empty = tmp_path / ".pre-commit-config.yaml"
    empty.write_text("repos: []\n")
    original = gate.CONFIG_PATH
    gate.CONFIG_PATH = empty
    try:
        assert gate.main() == 1
    finally:
        gate.CONFIG_PATH = original
