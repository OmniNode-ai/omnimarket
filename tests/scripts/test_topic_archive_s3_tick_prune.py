# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for scripts/topic_archive/topic-archive-s3-tick.sh's lab-host pruning.

Each tick stages a run on the lab host and, on a verified upload, only ever
deleted its own LOCAL (Mac-side) staging copy -- the lab-host copy under
``/data/omninode/hook-archive`` was never pruned (OMN-19513, ledger row
consent=docs/tracking/ROLLING_WORK_LEDGER.md:4073). These tests build a
disposable ROOT that mimics the real ``.onex_state/topic-archive-s3`` layout,
fake ``ssh``/``aws``/the archiver's python entrypoint on PATH so no network,
AWS or real lab host is touched, and drive the real tick script end to end.

``ssh`` is faked as "drop the connection flags and host, run the rest
locally" (a real ``bash -s --`` execs the staged stdin script; a quoted
remote command string is handed to ``bash -c``), with REMOTE_ROOT pointed at
a real local temp directory standing in for the lab host. This lets the
tick's own remote-enumeration and remote-delete commands run for real,
against a fake "remote".
"""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TICK_SCRIPT = REPO_ROOT / "scripts" / "topic_archive" / "topic-archive-s3-tick.sh"


@dataclass
class TickEnv:
    root: Path
    remote_root: Path
    rc_file: Path
    env: dict[str, str]
    tick: Path


_FAKE_SSH = """#!/bin/bash
set -euo pipefail
args=("$@")
while [[ "${args[0]:-}" == "-o" ]]; do
  args=("${args[@]:2}")
done
args=("${args[@]:1}")  # drop the host
if [[ "${#args[@]}" -eq 1 ]]; then
  exec bash -c "${args[0]}"
else
  exec "${args[@]}"
fi
"""

_FAKE_STAGER = """#!/bin/bash
# Stands in for lab-archive-to-staging.sh: just stages an empty verified run.
set -euo pipefail
OUT="${2:?out dir}"
mkdir -p "${OUT}"
: > "${OUT}/STAGED_OK"
"""


def _fake_aws(bucket: str) -> str:
    return f"""#!/bin/bash
set -euo pipefail
case "$1 $2" in
  "sts get-caller-identity")
    echo "arn:aws:sts::000000000000:assumed-role/test/test"
    exit 0
    ;;
  "s3api list-buckets")
    echo "{bucket}"
    exit 0
    ;;
esac
echo "fake aws: unhandled $*" >&2
exit 1
"""


def _fake_archiver(rc_file: Path) -> str:
    # Reads the exit code to use from rc_file each invocation, so a test can
    # flip a run from FAIL to PASS (or vice versa) between ticks.
    return f"""#!/bin/bash
set -euo pipefail
rc="$(cat {rc_file})"
echo '{{"verdict": "verified", "files": 1, "records": 1, "already_archived": 0, "sink": "test", "detail": ""}}'
exit "${{rc}}"
"""


def _chmod_x(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def tick_env(tmp_path: Path) -> TickEnv:
    """Build a disposable ROOT + PATH with every remote/AWS/archiver call faked."""
    root = tmp_path / ".onex_state" / "topic-archive-s3"
    root.mkdir(parents=True)
    remote_root = tmp_path / "remote" / "hook-archive"
    remote_root.mkdir(parents=True)
    release = root / "releases" / "testsha"
    (release / "scripts" / "topic_archive").mkdir(parents=True)
    (release / ".source-sha").write_text("testsha\n")
    (release / "scripts" / "topic_archive" / "lab-archive-to-staging.sh").write_text(
        _FAKE_STAGER
    )
    real_tick = TICK_SCRIPT.read_text()
    (release / "scripts" / "topic_archive" / "topic-archive-s3-tick.sh").write_text(
        real_tick
    )
    fake_venv_bin = release / ".venv" / "bin"
    fake_venv_bin.mkdir(parents=True)
    rc_file = tmp_path / "archiver_rc"
    rc_file.write_text("0\n")
    archiver = fake_venv_bin / "python"
    archiver.write_text(_fake_archiver(rc_file))
    _chmod_x(archiver)
    (root / "src").symlink_to(release, target_is_directory=True)
    for sub in ("staging", "reports", "uploaded", "logs"):
        (root / sub).mkdir()

    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh_stub = bindir / "ssh"
    ssh_stub.write_text(_FAKE_SSH)
    _chmod_x(ssh_stub)
    aws_stub = bindir / "aws"
    aws_stub.write_text(_fake_aws("test-bucket-fake"))
    _chmod_x(aws_stub)

    env = dict(os.environ)
    env["OMNI_HOME"] = str(tmp_path)  # ROOT = OMNI_HOME/.onex_state/topic-archive-s3
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["TOPIC_ARCHIVE_LAB_HOST"] = "fake-lab-host"
    env["TOPIC_ARCHIVE_REMOTE_ROOT"] = str(remote_root)
    env["AWS_PROFILE"] = "default"

    return TickEnv(
        root=root,
        remote_root=remote_root,
        rc_file=rc_file,
        env=env,
        tick=root / "src" / "scripts" / "topic_archive" / "topic-archive-s3-tick.sh",
    )


def _run_tick(tick_env: TickEnv) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(tick_env.tick)],
        env=tick_env.env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _seed_pending_run(remote_root: Path, name: str) -> Path:
    run = remote_root / name
    run.mkdir()
    (run / "some.jsonl.gz").write_bytes(b"x")
    (run / "STAGED_OK").touch()
    return run


def test_a_verified_upload_prunes_the_lab_host_run_dir(tick_env: TickEnv) -> None:
    """RED until the tick prunes: a verified (rc=0) run must vanish from the lab host."""
    pre_existing = _seed_pending_run(tick_env.remote_root, "sched-preexisting")
    tick_env.rc_file.write_text("0\n")

    result = _run_tick(tick_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not pre_existing.exists(), (
        "a verified staged run must be deleted from the lab host, not just "
        f"the Mac-side staging copy; stdout={result.stdout}"
    )
    # its own freshly-staged run (this tick's RUN=sched-<now>) verified too and
    # must also be gone -- only the fake remote root dir itself remains.
    assert list(tick_env.remote_root.iterdir()) == []


def test_a_failed_upload_never_prunes_the_lab_host_run_dir(tick_env: TickEnv) -> None:
    """An unverified/partial upload must leave the lab-host copy untouched."""
    pending = _seed_pending_run(tick_env.remote_root, "sched-willfail")
    tick_env.rc_file.write_text("1\n")

    result = _run_tick(tick_env)

    assert result.returncode != 0
    assert pending.exists(), "a failed upload must never delete the lab-host run"
    assert (pending / "STAGED_OK").exists()


def test_prune_targets_only_the_named_run_never_the_parent(tick_env: TickEnv) -> None:
    """The delete must be scoped to the one verified run dir, never a glob over
    the whole remote root (a sibling pending/failed run must survive)."""
    ok_run = _seed_pending_run(tick_env.remote_root, "sched-ok")
    # A second, unrelated directory with no STAGED_OK marker: never a pending
    # run, must never be touched by any delete this tick issues.
    untouched = tick_env.remote_root / "not-a-staged-run"
    untouched.mkdir()
    (untouched / "keepme").write_bytes(b"keep")
    tick_env.rc_file.write_text("0\n")

    result = _run_tick(tick_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not ok_run.exists()
    assert untouched.exists()
    assert (untouched / "keepme").exists()
