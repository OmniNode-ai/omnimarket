# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reopen-dev bump leaves nothing behind that a gate pins (OMN-26025).

The reopen-dev job of ``release-cut.yml`` used to commit ``pyproject.toml`` and
``uv.lock`` only. The work-contract corpus pins the sha256 of ``uv.lock`` in
every contract that names it, so the bump pull request failed the pins test
until a person repinned the contracts: dev stayed armed for hours.

These tests run the job's own shell text on a COPY of the tree, with a ``uv``
shim whose ``lock`` rewrites the self-version in ``uv.lock`` (the only thing
``uv lock`` changes for a version bump, and the only part that needs no network),
then stage what the job stages, and run the two gates a bump pull request meets
on the copy: the work-contract pins test and omnibase_core's release-identity
check.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-cut.yml"
CORPUS = Path("src/omnimarket/configs/work_contracts")
PINS_TEST = Path("tests/nodes/test_work_contract_pins_current.py")
RELEASE_IDENTITY = "omnibase_core.nodes.node_release_identity_check_compute.runtime_release_identity_check"
BASE_FILES = (
    Path("pyproject.toml"),
    Path("uv.lock"),
    Path("scripts/ci/post_release_dev_bump.py"),
    Path("src/omnimarket/handlers/work_contracts/repin.py"),
    PINS_TEST,
    Path("contracts/OMN-18010.yaml"),
)

UV_SHIM = """#!{python}
import re, sys, tomllib
from pathlib import Path
if sys.argv[1:] != ["lock"]:
    raise SystemExit(f"uv shim: only `uv lock` is simulated, got {{sys.argv[1:]}}")
version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
lock = Path("uv.lock")
text = lock.read_text()
new, n = re.subn(r'(name = "omnimarket"\\nversion = )"[^"]+"', rf'\\1"{{version}}"', text, count=1)
assert n == 1, "uv.lock has no omnimarket package entry"
lock.write_text(new)
"""


def _bump_step() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["reopen-dev"]["steps"]
    return next(s for s in steps if s.get("id") == "bump")


def _commit_step() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["reopen-dev"]["steps"]
    return next(s for s in steps if "git add" in s.get("run", ""))


def _pinned_paths() -> set[str]:
    """Every file path a corpus contract pins, so the copy carries exactly those."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("path"), str) and "sha256" in node:
                found.add(node["path"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for contract in sorted((REPO_ROOT / CORPUS / "contracts").glob("*.yaml")):
        walk(yaml.safe_load(contract.read_bytes()))
    return found


def _git(copy: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(copy), *args],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout


@pytest.fixture(scope="module")
def pristine(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the tree's bump-relevant files, committed and tagged as released."""
    dest = tmp_path_factory.mktemp("pristine") / "tree"
    wanted = {str(p) for p in BASE_FILES} | _pinned_paths()
    wanted |= {
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / CORPUS).rglob("*")
        if p.is_file()
    }
    for rel in sorted(wanted):
        src = REPO_ROOT / rel
        if src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    version = tomllib.loads((dest / "pyproject.toml").read_text())["project"]["version"]
    _git(dest, "init", "-q", "-b", "dev")
    _git(dest, "config", "user.email", "bump@example.invalid")
    _git(dest, "config", "user.name", "bump")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", f"release {version}")
    _git(dest, "tag", f"v{version}")
    _git(dest, "update-ref", "refs/remotes/origin/dev", "HEAD")
    return dest


@pytest.fixture
def copy(pristine: Path, tmp_path: Path) -> Path:
    """A private copy of the pristine copy for one test to bump and break."""
    dest = tmp_path / "tree"
    shutil.copytree(pristine, dest, symlinks=True)
    return dest


def _run_step(
    copy: Path, tmp_path: Path, step: dict[str, Any], env: dict[str, str]
) -> None:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "uv"
    shim.write_text(UV_SHIM.format(python=sys.executable), encoding="utf-8")
    shim.chmod(0o755)
    base_env = {
        **scrub_git_location_env(os.environ),
        "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(tmp_path / "gh_output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "gh_summary"),
        **env,
    }
    done = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=copy,
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, (
        f"step {step.get('name')!r} failed:\n{done.stdout}\n{done.stderr}"
    )


def reopen_dev(copy: Path, tmp_path: Path) -> str:
    """Run the job's bump step, then its staging, on the copy; return the target version."""
    released = _git(copy, "tag", "--list", "v*").split()[-1]
    _run_step(copy, tmp_path, _bump_step(), {"RELEASED_VERSION": released})
    target = tomllib.loads((copy / "pyproject.toml").read_text())["project"]["version"]
    lines = _commit_step()["run"].splitlines()
    first = next(
        i for i, line in enumerate(lines) if line.strip().startswith("git add")
    )
    last = next(
        i for i, line in enumerate(lines) if line.strip() == "git diff --exit-code"
    )
    done = subprocess.run(
        ["bash", "-e", "-c", "\n".join(lines[first : last + 1])],
        cwd=copy,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return target


def _pins_test(copy: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(PINS_TEST),
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            os.devnull,
            "--rootdir",
            str(copy),
        ],
        cwd=copy,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )


def _release_identity(copy: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            RELEASE_IDENTITY,
            "--root",
            str(copy),
            "--base",
            "origin/dev",
        ],
        cwd=copy,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )


def _uv_lock_pins(copy: Path) -> list[tuple[Path, str]]:
    pins = []
    for path in sorted((copy / CORPUS / "contracts").glob("*.yaml")):
        env = (
            (yaml.safe_load(path.read_bytes()).get("acceptance") or {})
            .get("spec", {})
            .get("environment")
        )
        if isinstance(env, dict) and env.get("path") == "uv.lock":
            pins.append((path, env["sha256"]))
    return pins


def test_reopen_dev_repin_moves_every_uv_lock_pin_in_the_commit(
    copy: Path, tmp_path: Path
) -> None:
    before = _uv_lock_pins(copy)
    assert len(before) >= 70, "the corpus pins uv.lock in 70 contracts today"
    reopen_dev(copy, tmp_path)
    digest = hashlib.sha256((copy / "uv.lock").read_bytes()).hexdigest()
    after = _uv_lock_pins(copy)
    assert [p for p, _ in after] == [p for p, _ in before]
    assert {sha for _, sha in after} == {digest}, (
        "a contract still holds the previous uv.lock hash"
    )
    staged = set(_git(copy, "diff", "--cached", "--name-only").split())
    assert {"pyproject.toml", "uv.lock"} <= staged
    assert {str(p.relative_to(copy)) for p, _ in after} <= staged
    assert _git(copy, "diff", "--name-only").split() == [], (
        "the job changed files it does not stage; they would be left out of the bump commit"
    )


def test_reopen_dev_repin_raises_the_floor_so_the_bound_checks_fail_at_base(
    copy: Path, tmp_path: Path
) -> None:
    target = reopen_dev(copy, tmp_path)
    floor = "(" + ", ".join(target.split(".")) + ")"
    for rel in (PINS_TEST, Path("contracts/OMN-18010.yaml")):
        assert f">= {floor}" in (copy / rel).read_text(), rel
    # At the merge base (the tagged release) the lock is one patch behind the floor,
    # so the pins test fails there: the base control has a bound check to fail.
    base_lock = _git(copy, "show", "HEAD:uv.lock")
    base_version = re.search(r'name = "omnimarket"\nversion = "([^"]+)"', base_lock)
    assert base_version is not None
    assert tuple(map(int, base_version[1].split("."))) < tuple(
        map(int, target.split("."))
    )


def test_bump_applied_to_copy_passes_the_pins_test_and_release_identity(
    copy: Path, tmp_path: Path
) -> None:
    reopen_dev(copy, tmp_path)
    pins = _pins_test(copy)
    assert pins.returncode == 0, pins.stdout + pins.stderr
    _git(
        copy,
        "-c",
        "user.email=b@example.invalid",
        "-c",
        "user.name=b",
        "commit",
        "-q",
        "-m",
        "bump",
    )
    identity = _release_identity(copy)
    assert identity.returncode == 0, identity.stdout + identity.stderr


def test_bump_applied_to_copy_fails_when_the_bump_skips_the_repin(
    copy: Path, tmp_path: Path
) -> None:
    """Red control: the job's text without its repin line is today's defect."""
    step = dict(_bump_step())
    step["run"] = "\n".join(
        line for line in step["run"].splitlines() if "repin" not in line
    )
    released = _git(copy, "tag", "--list", "v*").split()[-1]
    _run_step(copy, tmp_path, step, {"RELEASED_VERSION": released})
    pins = _pins_test(copy)
    assert pins.returncode != 0, (
        "a bump that skips the repin must fail the pins test on the copy"
    )
    assert (
        "input_pin_mismatch" in pins.stdout + pins.stderr
        or "environment pin" in pins.stdout
    )


def test_bump_applied_to_copy_fails_when_one_pin_is_left_behind(
    copy: Path, tmp_path: Path
) -> None:
    reopen_dev(copy, tmp_path)
    victim = sorted((copy / CORPUS / "contracts").glob("*.yaml"))[0]
    for path, sha in _uv_lock_pins(copy):
        victim = path
        text = path.read_text()
        path.write_text(text.replace(sha, "0" * 64))
        break
    pins = _pins_test(copy)
    assert pins.returncode != 0, (
        f"one stale pin ({victim.name}) must fail the pins test"
    )
