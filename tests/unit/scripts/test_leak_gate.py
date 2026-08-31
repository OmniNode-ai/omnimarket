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
EXPOSED_ID_GATE_REL = Path("scripts/validation/check_exposed_identifiers.py")
EXPOSED_ID_GATE_SRC = REPO_ROOT / EXPOSED_ID_GATE_REL
EXPOSED_ID_DENYLIST_REL = Path("scripts/validation/exposed_identifiers_denylist.json")
EXPOSED_ID_DENYLIST_SRC = REPO_ROOT / EXPOSED_ID_DENYLIST_REL


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
    target_exposed_id_gate = tmp_path / EXPOSED_ID_GATE_REL
    shutil.copy2(EXPOSED_ID_GATE_SRC, target_exposed_id_gate)
    target_exposed_id_gate.chmod(0o755)
    shutil.copy2(EXPOSED_ID_DENYLIST_SRC, tmp_path / EXPOSED_ID_DENYLIST_REL)
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
def test_generation_evidence_json_is_path_exempt(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    evidence = tmp_path / "docs" / "evidence" / "OMN-13294" / "proof.generation.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        '{"resolved_endpoint": "http://192.168.86.201:8000/v1/chat/completions"}\n'
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "generation evidence"], cwd=tmp_path, check=True
    )

    result = _run(tmp_path, "blocking", "all")
    assert result.returncode == 0, result.stdout


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


# --------------------------------------------------------------------------- #
# OMN-17369 -- the `staged` scope, i.e. the pre-commit surface.
#
# The hook runs with pass_filenames: false, so the scope argument IS the file
# selection. `diff` enumerates BASE_REF...HEAD -- already-committed files -- and
# therefore never sees the file being committed. Measured on dev b95e9d03: a
# staged file carrying a real forbidden literal produced `files_scanned=54
# findings=0` and the hook PASSED. These tests pin the fix so it cannot silently
# regress to a committed-files-only enumeration.
# --------------------------------------------------------------------------- #


def _stage(tmp_path: Path, rel: str, content: str) -> None:
    """Write `content` to `rel` and stage it WITHOUT committing."""
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "--", rel], cwd=tmp_path, check=True)


@pytest.mark.unit
def test_staged_scope_blocks_a_leak_that_is_staged_but_not_committed(
    tmp_path: Path,
) -> None:
    """AC2: the regression. This is the case `diff` scope silently passed."""
    _init_repo(tmp_path)
    _stage(tmp_path, "src/module.py", 'HOST = "192.168.86.201"\n')

    result = _run(tmp_path, "blocking", "staged")
    assert result.returncode == 1, (
        "staged scope passed a staged leak -- the OMN-17369 defect is back\n"
        f"{result.stdout}"
    )
    assert "src/module.py" in result.stdout


@pytest.mark.unit
def test_diff_scope_is_blind_to_staged_content(tmp_path: Path) -> None:
    """Pins WHY the hook may not use `diff`: that scope enumerates
    ``BASE_REF...HEAD``, so staged-but-uncommitted content is invisible to it.

    The base ref must actually exist for this to reproduce. Without it the script
    WARNs and falls back to scope=all, whose ``git ls-files -co`` DOES list staged
    files -- so a naive fixture makes `diff` look fine and hides the defect. That
    fallback is exactly why the bug survived review.
    """
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=tmp_path,
        check=True,
    )
    _stage(tmp_path, "src/module.py", 'HOST = "192.168.86.201"\n')

    result = _run(tmp_path, "blocking", "diff")
    assert "falling back to scope=all" not in result.stderr, (
        "fixture did not establish the base ref; the assertion below would be "
        "vacuous:\n" + result.stderr
    )
    assert "src/module.py" not in result.stdout, (
        "diff scope now sees staged files; if that is deliberate, delete this "
        "test and the OMN-17369 comments that cite it"
    )
    assert result.returncode == 0, result.stdout


@pytest.mark.unit
def test_staged_scope_ignores_unstaged_working_tree_changes(tmp_path: Path) -> None:
    """A leak sitting in the worktree but never `git add`-ed is not being
    committed, so the pre-commit surface must not block on it."""
    _init_repo(tmp_path)
    unstaged = tmp_path / "src" / "scratch.py"
    unstaged.parent.mkdir(parents=True, exist_ok=True)
    unstaged.write_text('HOST = "192.168.86.201"\n', encoding="utf-8")

    result = _run(tmp_path, "blocking", "staged")
    assert result.returncode == 0, result.stdout
    assert "findings=0" in result.stdout


@pytest.mark.unit
def test_staged_scope_with_nothing_staged_passes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    result = _run(tmp_path, "blocking", "staged")
    assert result.returncode == 0, result.stdout
    assert "findings=0" in result.stdout


@pytest.mark.unit
def test_staged_scope_honours_the_inline_annotation(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _stage(
        tmp_path,
        "src/module.py",
        'HOST = "192.168.86.201"  # onex-allow-internal-ip OMN-17369 '
        'reason="fixture proving the annotation survives the staged scope"\n',
    )

    result = _run(tmp_path, "blocking", "staged")
    assert result.returncode == 0, result.stdout


@pytest.mark.unit
def test_staged_scope_tolerates_a_staged_deletion(tmp_path: Path) -> None:
    """--diff-filter=ACMR must drop deletions: the path is gone from disk, and a
    missing path must not become a hard error."""
    _init_repo(tmp_path)
    doomed = tmp_path / "src" / "doomed.py"
    doomed.parent.mkdir(parents=True, exist_ok=True)
    doomed.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "add"], cwd=tmp_path, check=True)
    subprocess.run(["git", "rm", "-q", "--", "src/doomed.py"], cwd=tmp_path, check=True)

    result = _run(tmp_path, "blocking", "staged")
    assert result.returncode == 0, result.stdout


@pytest.mark.unit
def test_precommit_hook_uses_the_staged_scope(tmp_path: Path) -> None:
    """AC5: the enforcement surface itself. A correct script wired with the wrong
    scope is the whole defect, so the wiring is asserted, not assumed."""
    config = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "check_leaked_literals.sh blocking staged" in config, (
        "the leaked-literals pre-commit hook must run scope=staged; scope=diff "
        "cannot see the file being committed (OMN-17369)"
    )
    assert "check_leaked_literals.sh blocking diff" not in config, (
        "a leaked-literals hook is still wired to the staged-blind diff scope "
        "(OMN-17369)"
    )
