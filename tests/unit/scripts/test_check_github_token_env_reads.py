# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for scripts/ci/check_github_token_env_reads.py (OMN-12856).

Verifies:
(a) Resolution from a contract-sourced ref succeeds (no violation produced).
(b) An unresolved ref (raw env read) fails fast with a clear message naming the ref.
(c) Static audit: zero raw GitHub-token env reads remain in src/omnimarket/.
(d) Gate exits 1 on injected violations (enforce mode).
(e) Gate exits 0 in report mode even with violations.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "ci" / "check_github_token_env_reads.py"


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "check_github_token_env_reads", _SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_github_token_env_reads"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def gate_module() -> Any:
    return _load_module()


# ---------------------------------------------------------------------------
# (a) Clean source produces zero violations
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scan_clean_source_returns_empty(tmp_path: Path, gate_module: Any) -> None:
    """A file using contract_secret_ref + resolve_api_key produces no violations."""
    src = tmp_path / "src" / "omnimarket" / "nodes" / "handler.py"
    src.parent.mkdir(parents=True)
    src.write_text(
        textwrap.dedent(
            """
            from omnimarket.nodes.contract_topics import contract_secret_ref
            from omnimarket.inference.secret_store_resolver import resolve_api_key
            from pathlib import Path

            _CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

            def get_token() -> str:
                ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
                secret = resolve_api_key(ref)
                assert secret is not None
                return secret.get_secret_value()
            """
        ),
        encoding="utf-8",
    )
    # Create a fake .git marker so _find_repo_root returns tmp_path
    (tmp_path / ".git").mkdir()
    violations = gate_module.scan(tmp_path)
    assert violations == [], f"Expected no violations, got: {violations}"


# ---------------------------------------------------------------------------
# (b) Raw GH_PAT env read is detected
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "code_snippet",
    [
        'import os\ntoken = os.environ["GH_PAT"]\n',
        'import os\ntoken = os.environ.get("GH_PAT", "")\n',
        'import os\ntoken = os.getenv("GH_PAT")\n',
        'import os\ntoken = os.environ["GITHUB_TOKEN"]\n',
        'import os\ntoken = os.getenv("GH_TOKEN")\n',
    ],
)
def test_scan_detects_raw_env_read(
    tmp_path: Path, gate_module: Any, code_snippet: str
) -> None:
    """Each form of raw GitHub-token env read produces exactly one violation."""
    src = tmp_path / "src" / "omnimarket" / "nodes" / "bad_handler.py"
    src.parent.mkdir(parents=True)
    src.write_text(code_snippet, encoding="utf-8")
    (tmp_path / ".git").mkdir(exist_ok=True)
    violations = gate_module.scan(tmp_path)
    assert len(violations) == 1, (
        f"Expected 1 violation for snippet {code_snippet!r}, got {violations}"
    )


# ---------------------------------------------------------------------------
# (c) Static audit: zero raw GitHub-token env reads in src/omnimarket/
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_raw_github_token_env_reads_in_src(gate_module: Any) -> None:
    """Static audit: src/omnimarket/ contains zero raw GitHub-token env reads.

    This is the canonical OMN-12856 negative-audit proof. It fails if any
    production source re-introduces os.environ["GH_PAT"] or equivalent.
    """
    violations = gate_module.scan(_REPO_ROOT)
    assert violations == [], (
        "Raw GitHub-token env reads found in src/omnimarket/. "
        "Replace with contract_secret_ref + resolve_api_key (OMN-12856).\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# (d) Gate exits 1 on violations in enforce mode (default)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_gate_exits_1_on_violations_enforce_mode(
    tmp_path: Path, gate_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() returns 1 when violations exist and --report not passed."""
    src = tmp_path / "src" / "omnimarket" / "nodes" / "bad.py"
    src.parent.mkdir(parents=True)
    src.write_text('import os\ntoken = os.environ["GH_PAT"]\n', encoding="utf-8")
    (tmp_path / ".git").mkdir(exist_ok=True)

    # Patch _find_repo_root to return tmp_path
    monkeypatch.setattr(gate_module, "_find_repo_root", lambda: tmp_path)
    rc = gate_module.main([])
    assert rc == 1


# ---------------------------------------------------------------------------
# (e) Gate exits 0 in report mode even with violations
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_gate_exits_0_in_report_mode(
    tmp_path: Path, gate_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main(["--report"]) returns 0 regardless of violations."""
    src = tmp_path / "src" / "omnimarket" / "nodes" / "bad.py"
    src.parent.mkdir(parents=True)
    src.write_text('import os\ntoken = os.getenv("GH_PAT")\n', encoding="utf-8")
    (tmp_path / ".git").mkdir(exist_ok=True)

    monkeypatch.setattr(gate_module, "_find_repo_root", lambda: tmp_path)
    rc = gate_module.main(["--report"])
    assert rc == 0
