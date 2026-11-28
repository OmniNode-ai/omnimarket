# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26770: no work is blocked on a version; identity is checked on the release path.

The pre-commit hook ``check-release-identity`` runs in the branch context and the
ci.yml ``release-identity-gate`` job runs the release context only on a release ref
or tag. Both run omnibase_core's runtime at the sha pinned in
``.pre-commit-config.yaml``; these tests run that runtime, the way the hook and the
job do, on a throwaway repository whose pyproject version equals its latest tag.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_URL = "https://github.com/OmniNode-ai/omnibase_core"
STEP_NAME = "No code merged onto a published version without a bump (OMN-16344)"


def _hook() -> tuple[str, list[str]]:
    config = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    found = [
        (str(repo["rev"]), [str(a) for a in hook.get("args", [])])
        for repo in config["repos"]
        if repo["repo"] == CORE_URL
        for hook in repo.get("hooks") or []
        if hook["id"] == "check-release-identity"
    ]
    assert len(found) == 1
    return found[0]


def _gate_step() -> dict[str, object]:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["release-identity-gate"]["steps"]
    return next(s for s in steps if s.get("name") == STEP_NAME)


def _git(root: Path, *argv: str) -> None:
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(
        ["git", *identity, *argv],
        cwd=root,
        env=scrub_git_location_env(os.environ),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def equal_version_repo(tmp_path: Path) -> Path:
    """A repo at 1.0.0, tagged v1.0.0, with packaged source changed after the tag."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "example"\nversion = "1.0.0"\n'
    )
    (root / "src" / "example.py").write_text("X = 1\n")
    _git(root, "init", "-q", "-b", "dev")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "release 1.0.0")
    _git(root, "tag", "v1.0.0")
    (root / "src" / "example.py").write_text("X = 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change packaged source at the published version")
    return root


def _run_pinned(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    rev, _ = _hook()
    env = scrub_git_location_env(os.environ)
    return subprocess.run(
        [
            "uv", "run", "--no-project",
            "--with", f"omnibase-core @ git+{CORE_URL}@{rev}",
            "python", "-m",
            "omnibase_core.nodes.node_release_identity_check_compute."
            "runtime_release_identity_check",
            "--root", str(root), *args, "src/example.py",
        ],
        capture_output=True, text=True, env=env, check=False,
    )  # fmt: skip


def _gate_context(ref: str, base: str = "dev", head: str = "") -> str:
    """The context the ci.yml step computes for an event, run from its own script."""
    script = str(_gate_step()["run"])
    before_runtime = script.split("# OMN-21940")[0]
    result = subprocess.run(
        ["bash", "-c", before_runtime + '\necho "CONTEXT=${CONTEXT}"'],
        capture_output=True, text=True, check=False, cwd=REPO_ROOT,
        env={"PATH": os.environ["PATH"], "GITHUB_REF": ref, "BASE_REF": base,
             "HEAD_REF": head},
    )  # fmt: skip
    match = re.search(r"^CONTEXT=(\w+)$", result.stdout, re.M)
    assert match, result.stdout + result.stderr
    return match.group(1)


def test_hook_runs_in_branch_context_pinned_to_a_sha() -> None:
    rev, args = _hook()
    assert re.fullmatch(r"[0-9a-f]{40}", rev)
    assert args[args.index("--context") + 1] == "branch"
    assert "--root" in args


def test_ci_gate_reads_the_rev_the_hook_pins() -> None:
    script = str(_gate_step()["run"])
    assert "check-release-identity" in script
    assert "--context" in script
    assert not re.search(r"omnibase_core@[0-9a-f]{40}", script)


def test_branch_commit_at_equal_version_passes_with_the_hook_args(
    equal_version_repo: Path,
) -> None:
    _, args = _hook()
    args = [str(equal_version_repo) if a == "." else a for a in args]
    result = _run_pinned(equal_version_repo, args)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("ref", "base", "head"),
    [
        ("refs/pull/9/merge", "dev", "omn-26770-feature"),
        ("refs/heads/dev", "dev", ""),
        ("refs/heads/gh-readonly-queue/dev/pr-9", "dev", ""),
    ],
)
def test_ci_gate_context_on_a_branch_is_branch(ref: str, base: str, head: str) -> None:
    assert _gate_context(ref, base, head) == "branch"


@pytest.mark.parametrize(
    ("ref", "base", "head"),
    [
        ("refs/tags/v1.0.0", "dev", ""),
        ("refs/heads/main", "main", ""),
        ("refs/pull/9/merge", "main", "dev"),
        ("refs/pull/9/merge", "dev", "release/1.0.1"),
    ],
)
def test_ci_gate_context_on_a_release_ref_is_release(
    ref: str, base: str, head: str
) -> None:
    assert _gate_context(ref, base, head) == "release"


def test_release_ref_at_equal_version_is_refused(equal_version_repo: Path) -> None:
    context = _gate_context("refs/tags/v1.0.0")
    result = _run_pinned(equal_version_repo, ["--context", context])
    assert result.returncode == 1, result.stdout + result.stderr
