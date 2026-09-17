# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the release-identity gate (OMN-16344).

The gate forbids merging a packaged-source change onto an already-published
version string. It is the omnimarket port of the same gate in omnibase_infra
(OMN-13412) and omnibase_core (OMN-13411), and the recurrence guard for the
state this repo was actually found in: dev's ``project.version`` sitting at
0.4.8 — identical to the published v0.4.8 tag — while carrying seven commits of
``src/`` changes, so one version string named two distinct code states.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.version import Version

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_release_identity.py"

# Git environment variables that OVERRIDE both an explicit `--git-dir` flag and
# `cwd` (the OMN-14891 corruption class). Mirrors the local scrub idiom already
# used by handler_report_anchor_probe rather than importing
# omnibase_core.validators.no_unguarded_git_subprocess: that module is a
# test-scanning validator, and this repo's convention is to keep the remedy
# local instead of taking a runtime dependency on it.
_GIT_LOCATION_ENV_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)
_GIT_DISCOVERY_ENV_VARS: tuple[str, ...] = ("GIT_CEILING_DIRECTORIES",)


def _scrub_git_location_env() -> dict[str, str]:
    """Return a copy of the process env with git-location overrides removed.

    OMN-16584: after stripping any ambient ``GIT_CONFIG*`` override, re-add a
    NEUTRAL one unconditionally so a real user-level git config (e.g. a
    developer's ``~/.gitconfig`` carrying ``[tag] gpgsign = true``) is excluded
    from the subprocess `git` calls below, exactly like the deletions above
    exclude leaked location overrides. Without this, `git tag <name>` (no
    ``-a``/``-m``) in `_isolated_checkout` gets forced-annotated by a poisoned
    ``tag.gpgsign=true`` and opens ``$GIT_EDITOR`` for `TAG_EDITMSG` — hanging
    or failing ("fatal: no tag message?") under a non-interactive test runner.
    ``GIT_CONFIG_NOSYSTEM=1`` covers the system config the same way;
    ``GIT_EDITOR=true`` is a second line of defense for any other config path
    that still tries to launch an interactive editor.
    """
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("GIT_CONFIG"):
            del env[key]
    for key in (*_GIT_LOCATION_ENV_VARS, *_GIT_DISCOVERY_ENV_VARS):
        env.pop(key, None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_EDITOR"] = "true"
    return env


def _load_module(path: Path = _SCRIPT):
    spec = importlib.util.spec_from_file_location("check_release_identity", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod():
    return _load_module()


@pytest.mark.unit
def test_passes_when_version_ahead_of_published(mod, monkeypatch):
    """src/** changed, but the version is strictly ahead — gate passes."""
    monkeypatch.setattr(mod, "_read_pyproject_version", lambda: Version("0.4.9"))
    monkeypatch.setattr(mod, "_latest_published_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_packaged_source_changed", lambda *_args: True)
    assert mod.main(["--base", "origin/dev"]) == 0


@pytest.mark.unit
def test_fails_when_src_changed_and_version_equals_published(mod, monkeypatch):
    """src/** changed and the version equals the published wheel — gate FAILS.

    This is the exact state omnimarket dev was in before OMN-16344: seven
    commits of src/ changes sitting on 0.4.8, the same version as the published
    v0.4.8 wheel.
    """
    monkeypatch.setattr(mod, "_read_pyproject_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_latest_published_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_packaged_source_changed", lambda *_args: True)
    assert mod.main(["--base", "origin/dev"]) == 1


@pytest.mark.unit
def test_fails_when_src_changed_and_version_behind_published(mod, monkeypatch):
    """A version BEHIND the latest published tag is also a fail."""
    monkeypatch.setattr(mod, "_read_pyproject_version", lambda: Version("0.4.7"))
    monkeypatch.setattr(mod, "_latest_published_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_packaged_source_changed", lambda *_args: True)
    assert mod.main(["--base", "origin/dev"]) == 1


@pytest.mark.unit
def test_exempt_when_no_packaged_source_changed(mod, monkeypatch):
    """A docs/tests/CI-only diff is exempt — the published wheel is unaffected."""
    monkeypatch.setattr(mod, "_read_pyproject_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_latest_published_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_packaged_source_changed", lambda *_args: False)
    assert mod.main(["--base", "origin/dev"]) == 0


@pytest.mark.unit
def test_passes_when_no_published_tag_yet(mod, monkeypatch):
    """A repo with no published tags cannot alias a published version."""
    monkeypatch.setattr(mod, "_read_pyproject_version", lambda: Version("0.1.0"))
    monkeypatch.setattr(mod, "_latest_published_version", lambda: None)
    assert mod.main(["--base", "origin/dev"]) == 0


@pytest.mark.unit
def test_config_error_on_missing_version(mod, monkeypatch):
    """A missing project.version is a config error (exit 2), not a pass."""

    def _raise():
        raise ValueError("no project.version")

    monkeypatch.setattr(mod, "_read_pyproject_version", _raise)
    assert mod.main(["--base", "origin/dev"]) == 2


@pytest.mark.unit
def test_packaged_source_changed_detects_src_prefix(mod):
    """The src/ prefix triggers the bump requirement; non-src does not."""
    assert mod._packaged_source_changed(None, ["src/omnimarket/nodes/node_x/foo.py"])
    assert not mod._packaged_source_changed(
        None, ["docs/foo.md", "tests/test_x.py", ".github/workflows/ci.yml"]
    )


@pytest.mark.unit
def test_explicit_changed_file_overrides_base(mod, monkeypatch):
    """An explicit --changed-file list bypasses git diffing entirely."""
    monkeypatch.setattr(mod, "_read_pyproject_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_latest_published_version", lambda: Version("0.4.8"))
    # Explicit src file => changed => must be ahead => fails at 0.4.8.
    assert mod.main(["--changed-file", "src/omnimarket/foo.py"]) == 1
    # Explicit docs file => not changed => exempt => passes.
    assert mod.main(["--changed-file", "docs/foo.md"]) == 0


def _isolated_checkout(tmp_path: Path, *, published_tag: str) -> Path:
    """Stand the REAL script up in a throwaway repo whose tag set we own.

    ``check_release_identity`` derives its repo root from its own file location
    (``Path(__file__).resolve().parents[1]``) and shells out to ``git`` there,
    so copying the real script + the real ``pyproject.toml`` into
    ``<tmp>/scripts/`` + ``<tmp>/`` makes ``<tmp>`` the root it inspects. Every
    input the gate reads is then under the test's control — no tags are written
    into, or deleted from, the developer's actual checkout, and the assertions
    stay valid no matter which real version omnimarket is sitting on.
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(_SCRIPT, root / "scripts" / _SCRIPT.name)
    shutil.copy2(
        _SCRIPT.resolve().parents[1] / "pyproject.toml", root / "pyproject.toml"
    )

    # The identity and signing overrides are pinned per-command for the same
    # reason the env is scrubbed: the throwaway repo must not inherit the
    # developer's global git config. `_scrub_git_location_env` only clears
    # LOCATION variables, so a developer with `tag.gpgsign = true` set globally
    # would have this lightweight tag demand a message and die with
    # "fatal: no tag message?" (exit 128) -- a failure that never reproduces in
    # CI, which signs nothing.
    scrubbed_git_env = _scrub_git_location_env()
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "smoke",
        ],
        ["-c", "tag.gpgsign=false", "tag", published_tag],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, env=scrubbed_git_env)
    return root


def _run_gate(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the gate against ``root`` only.

    The scrub is load-bearing, not ceremony: GIT_DIR / GIT_WORK_TREE override
    ``cwd``, so an ambient one (a git hook exports exactly these — the
    OMN-14891 case) would make the gate's internal ``git tag --list`` read the
    developer's real checkout instead of the isolated repo, and the isolation
    this helper exists to provide would silently evaporate.
    """
    scrubbed_git_env = _scrub_git_location_env()
    return subprocess.run(
        [sys.executable, str(root / "scripts" / _SCRIPT.name), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
        env=scrubbed_git_env,
    )


@pytest.mark.unit
def test_live_invocation_smoke(tmp_path):
    """Real subprocess run of the real script, end to end: version ahead => 0.

    The isolated checkout owns its tag set outright rather than forcing a
    synthetic high tag into the real repo. ``_latest_published_version`` takes
    the MAX over all tags, so a synthetic tag only decides the comparison while
    it outranks every real tag — the moment a higher real tag is cut such a
    fixture goes inert and the assertion inverts on every open PR.
    """
    root = _isolated_checkout(tmp_path, published_tag="v0.0.1")

    result = _run_gate(root)

    assert result.returncode == 0, result.stderr
    assert "ahead of latest published" in result.stdout


@pytest.mark.unit
def test_live_invocation_fails_when_version_is_not_ahead(tmp_path):
    """Live negative: the gate must FAIL, not merely be absent, when behind.

    Exists-but-wrong, end to end through the real subprocess — a script that
    silently exited 0 on an un-bumped version would pass the positive smoke
    above and still let the whole failure class through.
    """
    root = _isolated_checkout(tmp_path, published_tag="v99.0.0")

    result = _run_gate(root)

    assert result.returncode == 1, result.stdout
    assert "is NOT ahead of the latest published version" in result.stderr


@pytest.mark.unit
def test_staged_mode_enforces_src_and_exempts_dev_only_metadata(tmp_path):
    """The local hook judges the exact staged snapshot, not the full tree."""
    root = _isolated_checkout(tmp_path, published_tag="v99.0.0")
    scrubbed_git_env = _scrub_git_location_env()
    source = root / "src" / "omnimarket" / "changed\nname.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", str(source.relative_to(root))],
        cwd=root,
        check=True,
        env=scrubbed_git_env,
    )
    staged_module = _load_module(root / "scripts" / _SCRIPT.name)

    assert staged_module._staged_files() == [str(source.relative_to(root))]

    source_result = _run_gate(root, "--staged")

    assert source_result.returncode == 1, source_result.stdout
    assert "is NOT ahead of the latest published version" in source_result.stderr

    subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        cwd=root,
        check=True,
        env=scrubbed_git_env,
    )
    with (root / "pyproject.toml").open("a", encoding="utf-8") as pyproject:
        pyproject.write("\n# test-only development metadata\n")
    subprocess.run(
        ["git", "add", "pyproject.toml"],
        cwd=root,
        check=True,
        env=scrubbed_git_env,
    )

    metadata_result = _run_gate(root, "--staged")

    assert metadata_result.returncode == 0, metadata_result.stderr
    assert "no packaged src/** change" in metadata_result.stdout


@pytest.mark.unit
def test_staged_mode_fails_closed_when_git_cannot_read_index(tmp_path):
    """A broken Git index is an error, never an exemption from the gate."""
    root = tmp_path / "not-a-repository"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(_SCRIPT, root / "scripts" / _SCRIPT.name)
    shutil.copy2(
        _SCRIPT.resolve().parents[1] / "pyproject.toml", root / "pyproject.toml"
    )

    result = _run_gate(root, "--staged")

    assert result.returncode == 2
    assert result.stderr.startswith("ERROR:")


@pytest.mark.unit
def test_isolated_checkout_hermetic_against_poisoned_tag_gpgsign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-16584 regression: a global ``[tag] gpgsign = true`` must not hang or
    fail ``_isolated_checkout``'s tag creation.

    A contractor reported 2 tests failing on his machine with this exact
    config: his real ``~/.gitconfig`` sets ``tag.gpgsign=true``, which forces
    the bare ``git tag <name>`` call in ``_isolated_checkout`` (no ``-a``/
    ``-m``) to upgrade to an annotated tag and open ``$GIT_EDITOR`` for
    ``TAG_EDITMSG`` -- failing non-interactively with "fatal: no tag
    message?".

    ``_scrub_git_location_env`` is what is supposed to prevent this
    (``GIT_CONFIG_GLOBAL=/dev/null`` + ``GIT_CONFIG_NOSYSTEM=1`` blind the
    subprocess to the real user-level config). Prove it actually works by
    simulating the contractor's exact machine state -- point ``HOME`` at a
    directory whose ``.gitconfig`` carries ``tag.gpgsign=true`` -- and confirm
    the live gate invocation still passes.
    """
    poisoned_home = tmp_path / "poisoned_home"
    poisoned_home.mkdir()
    (poisoned_home / ".gitconfig").write_text("[tag]\n\tgpgsign = true\n")
    monkeypatch.setenv("HOME", str(poisoned_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    root = _isolated_checkout(tmp_path, published_tag="v0.0.1")
    result = _run_gate(root)

    assert result.returncode == 0, result.stderr
    assert "ahead of latest published" in result.stdout


# ---------------------------------------------------------------------------
# OMN-18058: the empty-three-dot fallback stays MERGE-BASE anchored.
# ---------------------------------------------------------------------------


def _omn18058_git(repo: Path, *args: str) -> str:
    # OMN-14891: git exports GIT_DIR/GIT_WORK_TREE into every hook environment and
    # those OVERRIDE `-C`, so an unscrubbed fixture would mutate the real worktree.
    scrubbed_git_env = _scrub_git_location_env()
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrubbed_git_env,
    ).stdout


def _omn18058_stale_base_repo(tmp_path: Path) -> Path:
    """A branch with NO commits of its own, after which a peer advanced ``dev``.

    The peer's commit touches packaged source (``src/``), so the two-dot form
    attributes a packaged-source change to a branch that changed nothing.
    """
    origin = tmp_path / "omn18058-origin.git"
    work = tmp_path / "omn18058-work"
    scrubbed_git_env = _scrub_git_location_env()
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)],
        check=True,
        env=scrubbed_git_env,
    )
    subprocess.run(["git", "init", "-q", str(work)], check=True, env=scrubbed_git_env)
    _omn18058_git(work, "config", "user.email", "omn18058@example.invalid")
    _omn18058_git(work, "config", "user.name", "omn18058 fixture")
    _omn18058_git(work, "config", "commit.gpgsign", "false")
    _omn18058_git(work, "checkout", "-q", "-b", "dev")
    (work / "docs").mkdir(parents=True, exist_ok=True)
    (work / "docs" / "base.md").write_text("base\n", encoding="utf-8")
    _omn18058_git(work, "add", "-A")
    _omn18058_git(work, "commit", "-q", "-m", "base")
    _omn18058_git(work, "remote", "add", "origin", str(origin))
    _omn18058_git(work, "push", "-q", "origin", "dev")

    _omn18058_git(work, "checkout", "-q", "-b", "feature")
    _omn18058_git(work, "checkout", "-q", "dev")
    peer = work / "src" / "peer_pkg" / "landed_by_someone_else.py"
    peer.parent.mkdir(parents=True, exist_ok=True)
    peer.write_text("# a peer's packaged-source landing\n", encoding="utf-8")
    _omn18058_git(work, "add", "-A")
    _omn18058_git(work, "commit", "-q", "-m", "a peer's packaged-source landing")
    _omn18058_git(work, "push", "-q", "origin", "dev")

    _omn18058_git(work, "checkout", "-q", "feature")
    _omn18058_git(work, "fetch", "-q", "origin", "dev")
    return work


@pytest.mark.unit
def test_omn18058_empty_branch_diff_never_attributes_a_peers_packaged_source(
    mod, monkeypatch, tmp_path
):
    """OMN-18058: a branch with no commits of its own is exempt, not armed.

    The old fallback used the two-dot ``git diff origin/dev``, which describes
    the difference between two TREES -- so every ``src/`` file a peer landed on
    ``dev`` since the branch point was reported as this branch's change and the
    version gate fired on work the branch never did.
    """
    repo = _omn18058_stale_base_repo(tmp_path)

    # Positive controls, on this same fixture: the three-dot set really is empty
    # (so the fallback under test is the branch that executes), and the two-dot
    # form really does surface the peer's packaged-source file.
    assert _omn18058_git(repo, "diff", "--name-only", "origin/dev...HEAD").strip() == ""
    assert "src/peer_pkg/landed_by_someone_else.py" in _omn18058_git(
        repo, "diff", "--name-only", "origin/dev", "HEAD"
    )

    monkeypatch.setattr(mod, "_REPO_ROOT", repo)
    assert mod._packaged_source_changed("origin/dev", []) is False

    # ...and the fallback still sees this branch's own UNCOMMITTED packaged edit.
    own = repo / "src" / "own_pkg" / "mine.py"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("# uncommitted, mine\n", encoding="utf-8")
    _omn18058_git(repo, "add", "-A")
    assert mod._packaged_source_changed("origin/dev", []) is True


# ---------------------------------------------------------------------------
# OMN-18443 — the gate must read ONE clock.
#
# Measured on omnimarket#2601 (run 35107791574, job 104902570663, 2026-09-16):
# CI checked out ``refs/pull/2601/merge`` — the merge commit GitHub computed at
# TRIGGER time against base ``aa51cad2`` — whose tree carries pyproject
# 0.4.107, and the highest tag reachable from that base is v0.4.106. So the
# tree under evaluation was correctly versioned. The same checkout step then
# fetched ``+refs/tags/*:refs/tags/*`` at RUN time, which brought in v0.4.107
# and v0.4.108 published from lineages the tree has never seen, and the gate
# compared the trigger-time tree against the run-time tag list and failed it.
#
# The invariant the gate exists to protect is about the tree it is looking at:
# that tree's version must be ahead of every release the tree descends from.
# A release cut on a lineage this tree does not contain cannot be aliased by
# this tree's content, and the merge resolves that version line on its own
# (the branch never authored a bump, so the three-way merge takes dev's newer
# value). Anchoring the published set to ``git tag --merged`` puts both sides
# of the comparison on the same clock.
# ---------------------------------------------------------------------------


def _git_in(root: Path, *args: str) -> str:
    """Run git in ``root`` under the same scrubbed env the gate itself gets."""
    scrubbed_git_env = _scrub_git_location_env()
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=scrubbed_git_env,
    ).stdout.strip()


def _set_project_version(root: Path, version: str) -> None:
    """Rewrite ``[project].version`` in the throwaway repo's pyproject.toml."""
    path = root / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    with path.open("rb") as fh:
        current = tomllib.load(fh)["project"]["version"]
    needle = f'version = "{current}"'
    assert needle in text, f"pyproject version line {needle!r} not found verbatim"
    path.write_text(text.replace(needle, f'version = "{version}"', 1), encoding="utf-8")


def _tag_peer_release_off_this_lineage(root: Path, tag: str) -> None:
    """Cut ``tag`` on a lineage HEAD cannot reach — a PEER PR's release.

    This is the live topology, not an analogy: the peer's squash merge landed
    on dev and release-on-merge tagged that merge sha, so the tag's commit is
    not an ancestor of the commit this branch's tree was computed from.
    """
    branch = _git_in(root, "rev-parse", "--abbrev-ref", "HEAD")
    _git_in(root, "checkout", "-q", "-b", "peer")
    (root / "peer.txt").write_text("peer release\n", encoding="utf-8")
    _git_in(root, "add", "-A")
    _git_in(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "peer release",
    )
    _git_in(root, "-c", "tag.gpgsign=false", "tag", tag)
    _git_in(root, "checkout", "-q", branch)


def _isolated_race_checkout(
    tmp_path: Path, *, reachable_tag: str, unreachable_tag: str, version: str
) -> Path:
    """Throwaway repo in the exact state omnimarket#2601 was refused in."""
    root = _isolated_checkout(tmp_path, published_tag=reachable_tag)
    _tag_peer_release_off_this_lineage(root, unreachable_tag)
    _set_project_version(root, version)

    # Positive controls on the FIXTURE, so neither assertion below can pass
    # because the topology silently failed to build. An empty or unreachable-
    # tag-less repo would make the race test green for the wrong reason.
    all_tags = _git_in(root, "tag", "--list").split()
    merged_tags = _git_in(root, "tag", "--merged", "HEAD").split()
    assert unreachable_tag in all_tags, f"peer tag never created: {all_tags}"
    assert unreachable_tag not in merged_tags, (
        f"peer tag IS reachable from HEAD — fixture is not the race: {merged_tags}"
    )
    assert reachable_tag in merged_tags, f"base tag not reachable: {merged_tags}"
    return root


@pytest.mark.unit
def test_peer_release_cut_off_this_lineage_does_not_arm_the_gate(tmp_path):
    """AC1 falsifier: a release published after this tree must not refuse it.

    The tree declares 0.4.107 and the highest release it descends from is
    v0.4.106, so it is correctly versioned. v0.4.107 exists, cut from a peer's
    merge this tree does not contain. The gate must pass.
    """
    root = _isolated_race_checkout(
        tmp_path,
        reachable_tag="v0.4.106",
        unreachable_tag="v0.4.107",
        version="0.4.107",
    )

    result = _run_gate(root)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "ahead of latest published" in result.stdout


@pytest.mark.unit
def test_version_equal_to_a_reachable_release_still_fails(tmp_path):
    """AC2 positive control: the real invariant must not regress.

    Same repo, same unreachable peer tag — the only change is that the tree now
    declares a version equal to a release it DOES descend from. That is the
    aliasing the gate exists to refuse, and it must still be refused.
    """
    root = _isolated_race_checkout(
        tmp_path,
        reachable_tag="v0.4.106",
        unreachable_tag="v0.4.107",
        version="0.4.106",
    )

    result = _run_gate(root)

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "is NOT ahead of the latest published version" in result.stderr


@pytest.mark.unit
def test_version_behind_a_reachable_release_still_fails(tmp_path):
    """AC2 positive control, strictly-behind arm: unreachable tags never rescue."""
    root = _isolated_race_checkout(
        tmp_path,
        reachable_tag="v0.4.106",
        unreachable_tag="v0.4.107",
        version="0.4.105",
    )

    result = _run_gate(root)

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "is NOT ahead of the latest published version" in result.stderr


@pytest.mark.unit
def test_published_tags_are_anchored_to_the_evaluated_tree(mod, monkeypatch):
    """The collector asks for tags REACHABLE FROM HEAD, not every tag that exists."""
    seen: list[list[str]] = []

    def fake_git(args: list[str]) -> str:
        seen.append(list(args))
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        if args[:2] == ["tag", "--merged"]:
            return "v1.0.0\nv1.0.1"
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(mod, "_git", fake_git)

    assert mod._published_tags() == ["v1.0.0", "v1.0.1"]
    assert ["tag", "--merged", "HEAD"] in seen
    assert ["tag", "--list"] not in seen


@pytest.mark.unit
def test_shallow_clone_falls_back_to_the_full_tag_list(mod, monkeypatch):
    """Fail-CLOSED: ancestry is unknowable on a shallow clone.

    ``git tag --merged`` needs the tagged commits' ancestry present. A shallow
    clone can omit it and silently return FEWER tags, which is the permissive
    direction — exactly the OMN-17240 failure shape. When ancestry cannot be
    trusted the collector falls back to the whole tag list, which is the
    stricter answer.
    """
    seen: list[list[str]] = []

    def fake_git(args: list[str]) -> str:
        seen.append(list(args))
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "true"
        if args[:2] == ["tag", "--merged"]:
            raise AssertionError("must not ancestry-anchor on a shallow clone")
        return "v1.0.0\nv9.9.9"

    monkeypatch.setattr(mod, "_git", fake_git)

    assert mod._published_tags() == ["v1.0.0", "v9.9.9"]
    assert ["tag", "--list"] in seen
