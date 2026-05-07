# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-10580 reason="writes synthetic leaky files to test the leak-gate script"; test-literal-ok: fixture literals only
# test-literal-ok: fixture literals only
"""Unit tests for scripts/validation/check_leaked_literals.sh.

OMN-10554. Wave 0 (advisory). Wave 3 will extend this with blocking-mode
positive/negative cases per plan Task 8 acceptance.

Each test creates a fresh tmp git repo, drops the script in, drops a fixture
file with planted content, and invokes the script via subprocess from the
absolute repo-root path with cwd=tmp_path. Self-exemption rules and
allowlist-annotation rules are exercised directly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_REL = Path("scripts/validation/check_leaked_literals.sh")
SCRIPT_SRC = REPO_ROOT / SCRIPT_REL


def _init_repo(tmp_path: Path) -> Path:
    """Initialize a git repo at tmp_path with the leak gate script in place."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    target_script = tmp_path / SCRIPT_REL
    target_script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRIPT_SRC, target_script)
    target_script.chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return target_script


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT_REL), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.unit
def test_advisory_mode_clean_tree_returns_zero(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    result = _run(tmp_path, "advisory", "all")
    assert result.returncode == 0
    assert "findings=0" in result.stdout


@pytest.mark.unit
def test_advisory_mode_with_planted_leak_returns_zero_but_reports(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    leaky = tmp_path / "src" / "module.py"
    leaky.parent.mkdir(parents=True, exist_ok=True)
    leaky.write_text('HOST = "192.168.86.201"\n')
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "leak"], cwd=tmp_path, check=True)

    result = _run(tmp_path, "advisory", "all")
    assert result.returncode == 0  # advisory always exits 0
    assert "findings=1" in result.stdout
    assert "src/module.py" in result.stdout
    assert "192.168.86.201" in result.stdout


@pytest.mark.unit
def test_blocking_mode_clean_tree_returns_zero(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    result = _run(tmp_path, "blocking", "all")
    assert result.returncode == 0


@pytest.mark.unit
def test_blocking_mode_with_planted_leak_returns_one(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    leaky = tmp_path / "src" / "module.py"
    leaky.parent.mkdir(parents=True, exist_ok=True)
    leaky.write_text('HOST = "192.168.86.201"\n')
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "leak"], cwd=tmp_path, check=True)

    result = _run(tmp_path, "blocking", "all")
    assert result.returncode == 1
    assert "findings=1" in result.stdout


@pytest.mark.unit
def test_docs_path_with_valid_annotation_is_allowed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    doc = tmp_path / "docs" / "topology.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        "Postgres host: 192.168.86.201  "
        '<!-- # onex-allow-internal-ip OMN-10554 reason="docs example only" -->\n'
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "annotated"], cwd=tmp_path, check=True)

    result = _run(tmp_path, "blocking", "all")
    assert result.returncode == 0, (
        f"valid annotation should pass; got rc={result.returncode}, "
        f"stdout={result.stdout!r}"
    )


@pytest.mark.unit
def test_docs_path_with_bare_annotation_is_rejected(tmp_path: Path) -> None:
    """Plain `# onex-allow-internal-ip` (no ticket+reason) must not pass."""
    _init_repo(tmp_path)
    doc = tmp_path / "docs" / "topology.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("Postgres host: 192.168.86.201  <!-- # onex-allow-internal-ip -->\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "bare"], cwd=tmp_path, check=True)

    result = _run(tmp_path, "blocking", "all")
    assert result.returncode == 1
    assert "findings=1" in result.stdout


@pytest.mark.unit
def test_src_path_annotation_exempts(tmp_path: Path) -> None:
    """Annotations ARE honoured in src/ — env-var fallbacks with proper annotation are allowed."""
    _init_repo(tmp_path)
    leaky = tmp_path / "src" / "module.py"
    leaky.parent.mkdir(parents=True, exist_ok=True)
    leaky.write_text(
        'HOST = "192.168.86.201"  '
        '# onex-allow-internal-ip OMN-10554 reason="env-var fallback; override via HOST_ENV"\n'
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "annotated-src"], cwd=tmp_path, check=True)

    result = _run(tmp_path, "blocking", "all")
    assert result.returncode == 0


@pytest.mark.unit
def test_src_path_unannotated_is_blocked(tmp_path: Path) -> None:
    """src/ files without annotation are still blocked."""
    _init_repo(tmp_path)
    leaky = tmp_path / "src" / "module.py"
    leaky.parent.mkdir(parents=True, exist_ok=True)
    leaky.write_text('HOST = "192.168.86.201"\n')
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "bare-src"], cwd=tmp_path, check=True)

    result = _run(tmp_path, "blocking", "all")
    assert result.returncode == 1


@pytest.mark.unit
def test_self_exempt_files_do_not_trigger(tmp_path: Path) -> None:
    """The gate script itself contains the pattern catalog; that's not a leak."""
    _init_repo(tmp_path)
    # The script was copied during _init_repo; it contains '192.168.86.' in
    # comments and the LEAK_REGEX. The self-exemption must skip it.
    result = _run(tmp_path, "blocking", "all")
    assert result.returncode == 0


@pytest.mark.unit
def test_filename_with_spaces_is_handled(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    spaced_dir = tmp_path / "src" / "with space"
    spaced_dir.mkdir(parents=True, exist_ok=True)
    leaky = spaced_dir / "module.py"
    leaky.write_text('HOST = "192.168.86.201"\n')
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "spaced"], cwd=tmp_path, check=True)

    result = _run(tmp_path, "blocking", "all")
    assert result.returncode == 1
    assert "with space/module.py" in result.stdout


@pytest.mark.unit
def test_invalid_mode_returns_two(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    result = _run(tmp_path, "bogus", "all")
    assert result.returncode == 2


@pytest.mark.unit
def test_invalid_scope_returns_two(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    result = _run(tmp_path, "advisory", "bogus")
    assert result.returncode == 2
