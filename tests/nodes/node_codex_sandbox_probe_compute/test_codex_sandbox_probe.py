# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sandbox roots and write-probe regression coverage (OMN-21328)."""

from __future__ import annotations

import importlib
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.node_overlay_reader import NodeOverlayError

pytestmark = pytest.mark.unit

probe = importlib.import_module(
    "omnimarket.nodes.node_codex_sandbox_probe_compute.handlers.handler_codex_sandbox_probe"
)

FAKE_CODEX = """#!/bin/sh
if [ -n "$FAKE_CODEX_CALLS" ]; then printf '%%s %%s\\n' "$0" "$1" >> "$FAKE_CODEX_CALLS"; fi
case "$1" in
  login|exec) exit 0 ;;
  sandbox)
    shift
    roots=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -c) shift; case "$1" in sandbox_workspace_write.writable_roots=*) roots=${1#*=} ;; esac ;;
        --) shift; break ;;
      esac
      shift
    done
    echo "$roots" >> "$FAKE_CODEX_LOG"
    if [ "${#roots}" -gt %(limit)d ]; then echo "Error: Argument list too long (os error 7)" >&2; exit 1; fi
    exec "$@" ;;
esac
"""


LIMIT = 4000


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture repositories must not inherit the commit hook's repository state (GIT_DIR, GIT_INDEX_FILE ...)."""
    for name in subprocess.check_output(
        ["git", "rev-parse", "--local-env-vars"],
        text=True,
        env=scrub_git_location_env(os.environ),
    ).splitlines():
        monkeypatch.delenv(name, raising=False)


def registry(tmp_path: Path, clones: int) -> Path:
    home = tmp_path / "registry"
    for index in range(clones):
        subprocess.run(
            ["git", "init", "-q", str(home / f"repo{index:02d}")],
            check=True,
            env=scrub_git_location_env(os.environ),
        )
    home.mkdir(exist_ok=True)
    return home


def roots_program(
    tmp_path: Path,
    clones: int,
    *,
    priority: str = "",
    limit: int = LIMIT,
    with_codex: bool = True,
    program: str | None = None,
    fake_codex: str = FAKE_CODEX,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "codex"
    fake.write_text(fake_codex % {"limit": limit}, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    base = [str(tmp_path / "w"), str(tmp_path / "c")]
    for path in base:
        Path(path).mkdir(exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "FAKE_CODEX_LOG": str(tmp_path / "roots-codex.log"),
        "FAKE_CODEX_CALLS": str(tmp_path / "roots-codex-calls.log"),
        "CODEX_ROOTS_PRIORITY": priority,
        "TMPDIR": str(tmp_path),
    }
    args = [str(fake), json.dumps(base)] if with_codex else []
    if program is None:
        # The fake's profile budget models Linux even when the tests run on macOS.
        program = probe.CODEX_GIT_ROOTS_PROGRAM.replace(
            "sys.platform.startswith('linux')", "True"
        )
    return subprocess.run(
        [
            "python3",
            "-c",
            program,
            str(registry(tmp_path, clones)),
            "",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )


def test_the_roots_program_without_the_codex_binary_grants_every_clone(
    tmp_path: Path,
) -> None:
    done = roots_program(tmp_path, 12, with_codex=False)
    assert len(json.loads(done.stdout)) == 12 * 4
    assert "dropped" not in done.stderr


@pytest.mark.parametrize("linux", [True, False], ids=["linux", "macos"])
def test_the_roots_program_counts_a_sandbox_timeout_as_fitting(
    tmp_path: Path, linux: bool
) -> None:
    program = probe.CODEX_GIT_ROOTS_PROGRAM.replace(
        "timeout=30", "timeout=0.2"
    ).replace("sys.platform.startswith('linux')", str(linux))
    done = roots_program(
        tmp_path, 3, program=program, fake_codex="#!/bin/sh\nexec sleep 1\n"
    )
    assert done.returncode == 0
    assert set(json.loads(done.stdout)) == {
        str((tmp_path / "registry" / f"repo{index:02d}" / ".git" / relative).resolve())
        for index in range(3)
        for relative in ("objects", "refs", "logs/refs", "worktrees")
    }
    assert "dropped" not in done.stderr
    assert not list(tmp_path.glob("*.fit")), "the fitting scratch is removed"


def test_the_roots_program_skips_the_fit_off_linux(tmp_path: Path) -> None:
    program = probe.CODEX_GIT_ROOTS_PROGRAM.replace(
        "sys.platform.startswith('linux')", "False"
    )
    done = roots_program(tmp_path, 3, program=program)
    assert done.returncode == 0
    assert not (tmp_path / "roots-codex-calls.log").exists()
    assert set(json.loads(done.stdout)) == {
        str((tmp_path / "registry" / f"repo{index:02d}" / ".git" / relative).resolve())
        for index in range(3)
        for relative in ("objects", "refs", "logs/refs", "worktrees")
    }
    assert "dropped" not in done.stderr


def test_the_roots_program_keeps_the_lanes_own_repository_when_the_registry_overflows(
    tmp_path: Path,
) -> None:
    """repo11 sorts last; naming it as the lane's repository keeps it ahead of the clones that fit by name."""
    done = roots_program(tmp_path, 12, priority="repo11")
    kept = json.loads(done.stdout)
    assert 0 < len(kept) < 12 * 4
    assert any("/repo11/.git/worktrees" in root for root in kept)
    assert not any("/repo05/.git/" in root for root in kept)
    assert "dropped:" in done.stderr
    assert "repo05" in done.stderr
    assert "repo11" not in done.stderr.split("dropped:")[1]
    # Every kept root set is a whole clone: all four of its roots, never a partial grant.
    assert len(kept) % 4 == 0


def test_the_roots_program_leaves_room_for_the_spare_roots_and_cleans_its_scratch(
    tmp_path: Path,
) -> None:
    kept = json.loads(roots_program(tmp_path, 12).stdout)
    base = [str(tmp_path / "w"), str(tmp_path / "c")]
    spare_roots = (
        probe.CODEX_ROOTS_FIT_SPARE
    )  # the fitter's default reserve exceeds the pool probe's own headroom
    assert spare_roots > probe.CODEX_ROOTS_PROBE_HEADROOM
    assert len(json.dumps(base + kept)) + spare_roots * 4 <= LIMIT
    assert not list(tmp_path.glob("*.fit")), "the fitting scratch is removed"


def test_the_fitted_roots_leave_a_tenth_of_the_ceiling_free_for_the_lane(
    tmp_path: Path,
) -> None:
    """one lab host, 2026-10-23: 24 registry clones fitted to 23 made 109 roots, 2 spare roots under the profile ceiling, and the Codex
    probe failed with "Argument list too long" (CODEX-SANDBOX-OVERSIZE roots=109) so the host had 0 free Codex slots. A fit that
    hugs the ceiling flips with every path or worktree change; the fitted set plus the pool probe's own headroom must leave a
    tenth of the ceiling free."""
    limit = 6000
    kept = json.loads(roots_program(tmp_path, 24, limit=limit).stdout)
    base = [str(tmp_path / "w"), str(tmp_path / "c")]
    headroom = [
        str(tmp_path / "c.pad" / f"headroom-{i}")
        for i in range(probe.CODEX_ROOTS_PROBE_HEADROOM)
    ]
    assert 0 < len(kept) < 24 * 4
    assert len(kept) % 4 == 0
    assert len(json.dumps(base + kept + headroom)) <= limit * 0.9


def test_an_oversize_failure_names_the_largest_contributors(tmp_path: Path) -> None:
    """The brief's guess was an oversized variable; the cause was the roots. The failure line now names the roots, the argument's
    size and the repositories that contribute most, so the next reader needs no strace."""
    roots = [
        f"/o/{name}/.git/{part}"
        for name in ("alpha", "beta")
        for part in ("objects", "refs", "logs/refs", "worktrees")
    ]
    roots += ["/o/gamma/.git/refs", "/tmp/cache"]
    child = "import sys; print('Error: Argument list too long (os error 7)', file=sys.stderr); sys.exit(1)"
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            probe.CODEX_SANDBOX_PROBE_PY,
            str(tmp_path / "p.log"),
            "5",
            sys.executable,
            "-c",
            child,
            "sandbox_workspace_write.writable_roots=" + json.dumps(roots),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    tail = json.loads(done.stdout.split(" ", 1)[1])["log_tail"]
    assert "Argument list too long" in tail
    assert f"roots={len(roots)}" in tail
    assert "alpha:4" in tail
    assert "beta:4" in tail
    assert "gamma:1" in tail
    plain = subprocess.run(
        [
            sys.executable,
            "-c",
            probe.CODEX_SANDBOX_PROBE_PY,
            str(tmp_path / "q.log"),
            "5",
            sys.executable,
            "-c",
            "import sys; print('seatbelt', file=sys.stderr); sys.exit(1)",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert "OVERSIZE" not in plain.stdout, (
        "a failure that is not the argument ceiling adds nothing"
    )


def test_the_roots_grant_the_lane_lock_directory_and_nothing_above_it(
    tmp_path: Path,
) -> None:
    """one lab host, 2026-10-21: a Codex lane ended blocked with "Governed refresh cannot open
    the governed refresh lane-lock file: Read-only file system". omnibase_infra's lane_lock fixes that directory
    (every process must agree on one lock path), so the sandbox grants it: created by the roots lines and passed by its real path,
    while both parent directories stay read-only."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    home = tmp_path / "home"
    home.symlink_to(real_home, target_is_directory=True)
    omni = registry(tmp_path, 1)
    work, ctx = tmp_path / "w", tmp_path / "c"
    work.mkdir()
    ctx.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # codex_roots_lines reads cache variables with bash -lc; keep the host's profile out of the test.
    bash = bin_dir / "bash"
    bash.write_text(
        '#!/bin/sh\nif [ "$1" = -lc ]; then shift; set -- -c "$@"; fi\nexec /bin/bash --noprofile --norc "$@"\n',
        encoding="utf-8",
    )
    bash.chmod(bash.stat().st_mode | stat.S_IXUSR)
    values = {
        "W": work,
        "C": ctx,
        "GC": "",
        "ONEX_REGISTRY_ROOT": omni,
        "ONEX_STATE_DIR": omni / ".onex_state",
        "ONEX_WORKTREES_ROOT": omni / "omni_worktrees",
        "ONEX_LANE": "lanelock-probe",
    }
    script = "\n".join(
        [
            *(f"{k}={shlex.quote(str(v))}" for k, v in values.items()),
            *probe.codex_roots_lines(private_data="repo-private-data"),
            'printf "%s\\n" "$CXR"',
        ]
    )
    done = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            "HOME": str(home),
            "PATH": f"{bin_dir}:{Path(sys.executable).parent}:/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    )
    assert done.returncode == 0, (done.stdout, done.stderr)
    roots = [Path(root) for root in json.loads(done.stdout)]
    lane_locks = real_home / ".omnibase" / "state" / "lane-locks"
    assert lane_locks.is_dir(), (
        "the roots lines create the lane-lock directory before Codex resolves its permissions"
    )
    assert (
        lane_locks in roots
    )  # by its real path: Codex refuses a root with a symlink component
    for wider in (
        real_home / ".omnibase" / "state",
        real_home / ".omnibase",
        home / ".omnibase" / "state",
        home / ".omnibase",
    ):
        assert wider not in roots


def test_the_roots_grant_the_uv_data_directory_and_nothing_above_it(
    tmp_path: Path,
) -> None:
    """OMN-21396: uv's data directory is created and granted by its real path alone."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    home = tmp_path / "home"
    home.symlink_to(real_home, target_is_directory=True)
    omni = registry(tmp_path, 1)
    work, ctx = tmp_path / "w", tmp_path / "c"
    work.mkdir()
    ctx.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # codex_roots_lines reads cache variables with bash -lc; keep the host's profile out of the test.
    bash = bin_dir / "bash"
    bash.write_text(
        '#!/bin/sh\nif [ "$1" = -lc ]; then shift; set -- -c "$@"; fi\nexec /bin/bash --noprofile --norc "$@"\n',
        encoding="utf-8",
    )
    bash.chmod(bash.stat().st_mode | stat.S_IXUSR)
    values = {
        "W": work,
        "C": ctx,
        "GC": "",
        "ONEX_REGISTRY_ROOT": omni,
        "ONEX_STATE_DIR": omni / ".onex_state",
        "ONEX_WORKTREES_ROOT": omni / "omni_worktrees",
        "ONEX_LANE": "uv-data-probe",
    }
    script = "\n".join(
        [
            *(f"{k}={shlex.quote(str(v))}" for k, v in values.items()),
            *probe.codex_roots_lines(private_data="repo-private-data"),
            'printf "%s\\n" "$CXR"',
        ]
    )
    done = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            "HOME": str(home),
            "PATH": f"{bin_dir}:{Path(sys.executable).parent}:/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    )
    assert done.returncode == 0, (done.stdout, done.stderr)
    roots = [Path(root) for root in json.loads(done.stdout)]
    uv_data = real_home / ".local" / "share" / "uv"
    assert uv_data.is_dir(), (
        "the roots lines create the uv data directory before Codex resolves its permissions"
    )
    assert uv_data in roots
    for wider in (
        real_home / ".local" / "share",
        real_home / ".local",
        home / ".local" / "share" / "uv",
    ):
        assert wider not in roots


def run_probe_line(
    tmp_path: Path, roots: list[Path]
) -> subprocess.CompletedProcess[str]:
    """Run the runner's own write-probe line with ``roots`` as ``$CXR`` and the fake sandbox that starts with any root."""
    fake = tmp_path / "codex"
    fake.write_text(FAKE_CODEX % {"limit": 10**6}, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    context = tmp_path / "c"
    context.mkdir(exist_ok=True)
    env = {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "CX": str(fake),
        "C": str(context),
        "CXR": json.dumps([str(root) for root in roots]),
        "FAKE_CODEX_LOG": str(tmp_path / "line-codex.log"),
    }
    return subprocess.run(
        ["/bin/sh", "-c", probe.codex_sandbox_probe_line()],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )


def test_the_write_probe_creates_a_root_that_is_missing_before_it_writes(
    tmp_path: Path,
) -> None:
    """one lab host, 2026-10-18: the pool refused Codex with ``[Errno 2] No such file or directory: '.../codex-sandbox-probe/.codex-sandbox-probe-x'``.
    The probe's own directory had vanished between its mkdir and the write (a second probe's exit trap removes the shared directory),
    and the write program only wrote. The probe line now creates every root outside the sandbox first."""
    present = tmp_path / "present"
    present.mkdir()
    done = run_probe_line(tmp_path, [present])
    assert done.returncode == 0, (
        done.stdout,
        done.stderr,
    )  # positive control: the probe passes when every root exists
    missing = tmp_path / "worktrees" / "codex-sandbox-probe"
    assert not missing.exists()
    done = run_probe_line(tmp_path, [present, missing])
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert missing.is_dir(), (
        "the probe's file is gone and the directory is left for its owner to remove"
    )
    assert not list(missing.iterdir()), (
        "the probe's file is gone and the directory is left for its owner to remove"
    )


def test_a_write_failure_names_the_step_that_failed(tmp_path: Path) -> None:
    """A root the sandbox cannot see or cannot write is named with the step (``exists`` or ``write``), so the refusal says which."""
    gone = tmp_path / "gone"
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            probe.CODEX_WRITE_PROBE_PROGRAM,
            json.dumps([str(gone)]),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert done.returncode == 1
    assert "step=exists" in done.stderr
    assert f"path={gone}" in done.stderr
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("x", encoding="utf-8")
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            probe.CODEX_WRITE_PROBE_PROGRAM,
            json.dumps([str(not_a_directory)]),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert done.returncode == 1
    assert "step=write" in done.stderr
    assert f"path={not_a_directory}" in done.stderr


def test_two_pool_probes_never_share_a_probe_directory() -> None:
    """The second cause of the one lab host failure: every probe used the lane name ``codex-sandbox-probe``, so one probe's exit trap
    ``rmdir`` removed the directory another was about to write into. Each probe now names its own."""
    assert "ONEX_LANE=codex-sandbox-probe-$$" in probe.codex_roots_probe_sh(
        private_data="repo-private-data"
    )
    assert "ONEX_LANE=codex-sandbox-probe;" not in probe.codex_roots_probe_sh(
        private_data="repo-private-data"
    )


@pytest.fixture
def repos_overlay(tmp_path: Path) -> dict[str, str]:
    overlay = tmp_path / "repos" / "overlay.yaml"
    overlay.parent.mkdir()
    overlay.write_text(
        "plugin_source: repo-plugin\n"
        "private_data: repo-private-data\n"
        "knowledge_base: repo-kb\n"
        "deploy: repo-deploy\n"
        "private_data_home_envs: [PRIVATE_DATA_HOME]\n"
        "knowledge_base_home_env: KB_HOME\n"
        "beside_root: [private_data, knowledge_base]\n",
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def test_roots_program_grants_configured_sibling_first(tmp_path: Path) -> None:
    root = registry(tmp_path, 1)
    sibling = tmp_path / "repo-private-data"
    subprocess.run(
        ["git", "init", "-q", str(sibling)],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    expected = [
        str((sibling / ".git" / relative).resolve())
        for relative in ("objects", "refs", "logs/refs", "worktrees")
    ]

    def run(beside: str | None) -> list[str]:
        env = {
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        if beside is not None:
            env["CODEX_ROOTS_BESIDE"] = beside
        done = subprocess.run(
            [sys.executable, "-c", probe.CODEX_GIT_ROOTS_PROGRAM, str(root), ""],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(done.stdout)

    assert run("repo-private-data")[:4] == expected
    assert set(expected).isdisjoint(run(None))


def test_missing_private_data_names_overlay_setting() -> None:
    with pytest.raises(NodeOverlayError, match=r"repos\.private_data"):
        probe.codex_roots_lines(environ={})


def test_private_data_is_read_from_shared_overlay(
    repos_overlay: dict[str, str],
) -> None:
    assert "export CODEX_ROOTS_BESIDE=repo-private-data" in probe.codex_roots_lines(
        environ=repos_overlay
    )


def test_explicit_private_data_wins_over_overlay(repos_overlay: dict[str, str]) -> None:
    lines = probe.codex_roots_lines(private_data="repo-explicit", environ=repos_overlay)
    assert "export CODEX_ROOTS_BESIDE=repo-explicit" in lines
    assert "export CODEX_ROOTS_BESIDE=repo-private-data" not in lines
    assert "CODEX_ROOTS_BESIDE=repo-explicit" in probe.codex_roots_probe_sh(
        private_data="repo-explicit", environ=repos_overlay
    )


def test_import_does_not_read_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    importlib.reload(probe)


def test_registry_root_variable_is_configurable() -> None:
    for lines in (
        probe.codex_roots_lines(
            private_data="repo-private-data", registry_root_var="OTHER_ROOT"
        ),
        probe.landing_worker_roots_lines(registry_root_var="OTHER_ROOT"),
    ):
        rendered = "\n".join(lines)
        assert "$OTHER_ROOT" in rendered
        assert "$ONEX_REGISTRY_ROOT" not in rendered
    lines = probe.codex_roots_lines("repo-a", private_data="repo-private-data")
    assert lines[0] == "export CODEX_ROOTS_PRIORITY=repo-a"
    assert lines.index("export CODEX_ROOTS_BESIDE=repo-private-data") < next(
        index for index, line in enumerate(lines) if line.startswith("GXR=")
    )


@pytest.mark.parametrize(
    "name", ["", "bad-name", "lower", "1ROOT", "ROOT;exit", "ROOT\n"]
)
def test_invalid_registry_root_variable_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="registry_root_var"):
        probe.codex_roots_lines(
            private_data="repo-private-data", registry_root_var=name
        )
    with pytest.raises(ValueError, match="registry_root_var"):
        probe.landing_worker_roots_lines(registry_root_var=name)


@pytest.mark.parametrize(
    "name", ["", "../repo", "repo/name", "repo data", "repo;exit", "repo\n"]
)
def test_invalid_private_data_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="private_data"):
        probe.codex_roots_lines(private_data=name, environ={})
    with pytest.raises(ValueError, match="private_data"):
        probe.codex_roots_probe_sh(private_data=name, environ={})


def test_missing_overlay_field_names_file(tmp_path: Path) -> None:
    overlay = tmp_path / "repos" / "overlay.yaml"
    overlay.parent.mkdir()
    overlay.write_text("plugin_source: repo-plugin\n", encoding="utf-8")
    with pytest.raises(NodeOverlayError, match=r"repos\.private_data") as exc:
        probe.codex_roots_lines(environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert str(overlay) in str(exc.value)


def test_invalid_overlay_private_data_is_refused(repos_overlay: dict[str, str]) -> None:
    overlay = Path(repos_overlay["ONEX_SKILL_OVERLAY_ROOTS"]) / "repos" / "overlay.yaml"
    overlay.write_text("private_data: ../repo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="private_data"):
        probe.codex_roots_lines(environ=repos_overlay)


# OMN-21279: a lane in a clone must be able to run a git fetch there. The fetch writes FETCH_HEAD (and packed-refs, shallow, gc.pid)
# at the top of the common git dir, which the objects/refs/logs/worktrees roots do not cover.
def git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def commit_repo(path: Path) -> Path:
    git("init", "-q", str(path))
    git("commit", "-q", "--allow-empty", "-m", "seed", cwd=path)
    return path


def lane_roots(tmp_path: Path, common: Path) -> list[str]:
    """The roots the program prints for a lane whose worktree has ``common`` as its common git dir (no codex fit)."""
    (tmp_path / "registry").mkdir(exist_ok=True)
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            probe.CODEX_GIT_ROOTS_PROGRAM,
            str(tmp_path / "registry"),
            str(common),
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(done.stdout)


def covered(path: Path, roots: list[str]) -> bool:
    return any(path == Path(root) or Path(root) in path.parents for root in roots)


def test_the_roots_cover_the_common_git_dir_of_a_worktree_in_another_clone(
    tmp_path: Path,
) -> None:
    clone = commit_repo(tmp_path / "clone")
    worktree = tmp_path / "lane" / "wt"
    git("worktree", "add", "-q", "--detach", str(worktree), "HEAD", cwd=clone)
    common = Path(
        git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=worktree)
    ).resolve()
    assert common == (clone / ".git").resolve()
    roots = lane_roots(tmp_path, common)
    for written in ("FETCH_HEAD", "packed-refs", "shallow", "gc.pid"):
        assert covered(common / written, roots), f"{written} is read-only in {roots}"
    assert str(common) in roots
    # Nothing wider: every root is the common dir or sits inside it, never the clone's source tree, a parent or $HOME.
    assert all(Path(root) == common or common in Path(root).parents for root in roots)
    assert not covered(clone / "README", roots)
    assert not covered(Path.home() / ".bashrc", roots)


def test_the_roots_do_not_grant_a_registry_peers_whole_git_dir(
    tmp_path: Path,
) -> None:
    lane = commit_repo(tmp_path / "lane-clone")
    peer = tmp_path / "registry" / "peer"
    commit_repo(peer)
    roots = lane_roots(tmp_path, lane / ".git")
    assert str((lane / ".git").resolve()) in roots
    assert not covered((peer / ".git").resolve() / "FETCH_HEAD", roots)
    assert not covered((peer / ".git").resolve() / "HEAD", roots)
    assert covered((peer / ".git").resolve() / "objects" / "x", roots)


def test_the_roots_cover_the_objects_dir_a_clone_reaches_through_alternates(
    tmp_path: Path,
) -> None:
    source = commit_repo(tmp_path / "source")
    clone = commit_repo(tmp_path / "clone")
    alternate = (source / ".git" / "objects").resolve()
    info = clone / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(
        f"{alternate}\n../../../source/.git/objects\n", encoding="utf-8"
    )
    roots = lane_roots(tmp_path, clone / ".git")
    assert str(alternate) in roots
    assert covered(alternate / "pack" / "x", roots)
    # The alternate's refs, HEAD and worktrees stay outside the grant.
    assert not covered(source / ".git" / "refs" / "heads" / "main", roots)
    assert not covered(source / ".git" / "HEAD", roots)
    assert not covered(source / ".git" / "worktrees" / "x", roots)


def test_alternates_that_are_not_objects_directories_are_not_granted(
    tmp_path: Path,
) -> None:
    clone = commit_repo(tmp_path / "clone")
    info = clone / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    wide = tmp_path / "elsewhere"
    wide.mkdir()
    not_a_repository = tmp_path / "not-a-repository" / "objects"
    not_a_repository.mkdir(parents=True)
    (info / "alternates").write_text(
        f"# a comment\n/\n{Path.home()}\n{wide}\n{tmp_path / 'missing' / 'objects'}\n{not_a_repository}\n",
        encoding="utf-8",
    )
    roots = lane_roots(tmp_path, clone / ".git")
    assert "/" not in roots
    assert str(Path.home()) not in roots
    assert str(wide) not in roots
    assert str(not_a_repository) not in roots
    assert not any(root.startswith(str(tmp_path / "missing")) for root in roots)
    assert not covered(wide / "file", roots)
