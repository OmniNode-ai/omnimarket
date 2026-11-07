# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Guard the digest-pinned TruffleHog secret scan (OMN-23008).

The scanner runs from an immutable image digest, never a tag, and each step
hands the scanner the arguments the trufflesecurity/trufflehog action built:
the same commit range, ``--fail --no-update --github-actions`` and the same
extra arguments. The drift workflow reads the same pinned value.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

_WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"
_SCAN = _WORKFLOWS / "security-scan.yml"
_DRIFT = _WORKFLOWS / "trufflehog-digest-drift.yml"
_DIGEST_RE = re.compile(r"ghcr\.io/trufflesecurity/trufflehog@sha256:[0-9a-f]{64}")
_FIXED_ARGS = [
    "--fail",
    "--no-update",
    "--github-actions",
    "--only-verified",
    "--exclude-detectors=lob",
]


def _workflow(path: Path) -> Any:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def _scan_step(name: str) -> Any:
    steps = _workflow(_SCAN)["jobs"]["secret-scan"]["steps"]
    (step,) = [s for s in steps if s.get("name") == name]
    return step


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def _run_step(tmp_path: Path, name: str, env: dict[str, str]) -> tuple[int, list[str]]:
    """Run a scan step's script in a two-commit repo with a recording docker.

    ``{base}`` and ``{head}`` in an env value are the repo's two commits.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for n in ("one", "two"):
        (repo / "f").write_text(n)
        _git(repo, "add", "f")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", n)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "argv"
    docker = bindir / "docker"
    docker.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {record}\n')
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    image = str(_workflow(_SCAN)["env"]["TRUFFLEHOG_IMAGE"])
    step = _scan_step(name)
    step_env = {k: str(v) for k, v in dict(step.get("env", {})).items()}
    base, head = _git(repo, "rev-parse", "HEAD~1", "HEAD").split("\n")
    resolved = {
        k: env.get(k, v).format(base=base, head=head) for k, v in step_env.items()
    }
    proc = subprocess.run(
        ["bash", "-e", "-c", str(step["run"])],
        cwd=repo,
        env={
            **scrub_git_location_env(os.environ),
            **resolved,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "TRUFFLEHOG_IMAGE": image,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    argv = record.read_text().split("\n")[:-1] if record.exists() else []
    if argv:
        argv.append(f"base={base}")
        argv.append(f"head={head}")
    return proc.returncode, argv


@pytest.mark.unit
def test_scanner_image_is_a_digest_and_the_action_is_gone() -> None:
    code = [
        line
        for line in _SCAN.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert not [line for line in code if "uses: trufflesecurity" in line]
    assert not [line for line in code if "trufflehog:" in line]
    image = _workflow(_SCAN)["env"]["TRUFFLEHOG_IMAGE"]
    assert _DIGEST_RE.fullmatch(str(image)), image


def _scanner_args(argv: list[str]) -> list[str]:
    """The arguments after the image, with the trailing base/head markers cut."""
    assert _DIGEST_RE.fullmatch(argv[argv.index("-w") + 2])
    return argv[argv.index("-w") + 3 : -2]


@pytest.mark.unit
def test_push_scans_before_to_after_with_the_action_flags(tmp_path: Path) -> None:
    rc, argv = _run_step(
        tmp_path,
        "TruffleHog Secret Scanning (push diff)",
        {"BASE": "{base}", "HEAD": "{head}"},
    )
    assert rc == 0
    base, head = argv[-2].removeprefix("base="), argv[-1].removeprefix("head=")
    assert _scanner_args(argv) == [
        "git",
        "file:///tmp/",
        "--since-commit",
        base,
        "--branch",
        head,
        *_FIXED_ARGS,
    ]


@pytest.mark.unit
def test_push_with_the_same_base_and_head_fails(tmp_path: Path) -> None:
    rc, argv = _run_step(
        tmp_path,
        "TruffleHog Secret Scanning (push diff)",
        {"BASE": "{head}", "HEAD": "{head}"},
    )
    assert rc == 1
    assert argv == []


@pytest.mark.unit
def test_pull_request_scans_base_to_head(tmp_path: Path) -> None:
    rc, argv = _run_step(
        tmp_path,
        "TruffleHog Secret Scanning (full scan)",
        {
            "EVENT_NAME": "pull_request",
            "PR_BASE_SHA": "b" * 40,
            "PR_HEAD_SHA": "c" * 40,
        },
    )
    assert rc == 0
    assert _scanner_args(argv) == [
        "git",
        "file:///tmp/",
        "--since-commit",
        "b" * 40,
        "--branch",
        "c" * 40,
        *_FIXED_ARGS,
    ]


@pytest.mark.unit
@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_schedule_and_dispatch_scan_full_history(tmp_path: Path, event: str) -> None:
    rc, argv = _run_step(
        tmp_path,
        "TruffleHog Secret Scanning (full scan)",
        {"EVENT_NAME": event, "PR_BASE_SHA": "", "PR_HEAD_SHA": ""},
    )
    assert rc == 0
    assert _scanner_args(argv) == [
        "git",
        "file:///tmp/",
        "--since-commit",
        "",
        "--branch",
        "",
        *_FIXED_ARGS,
    ]


@pytest.mark.unit
def test_drift_check_reads_the_pinned_image_and_can_open_issues() -> None:
    drift = _workflow(_DRIFT)
    # PyYAML reads the bare `on:` key as True.
    triggers = drift[True]
    assert "schedule" in triggers
    assert "workflow_dispatch" in triggers
    job = drift["jobs"]["digest-drift"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["permissions"]["issues"] == "write"
    script = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "TRUFFLEHOG_IMAGE" in script
    assert "security-scan.yml" in script
    assert "trufflehog:latest" in script
