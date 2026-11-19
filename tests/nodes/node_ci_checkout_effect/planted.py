# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A planted deployed overlay document of node_ci_checkout_effect, shared by its tests (OMN-24287)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

HOST = "test-host"


def deployment_document(
    mirrors: Path,
    *,
    host: str = HOST,
    sync_on_miss: bool = False,
    sync_command: tuple[str, ...] = (),
    home: Path | None = None,
) -> dict[str, Any]:
    """The overlay document for one host whose bare mirrors sit under ``mirrors``, served over file://."""
    return {
        "github_owner": "OmniNode-ai",
        "sources": [
            {
                "name": "bare-mirror",
                "path_template": "{mirror_root}/{repo_name}.git",
                "serve_url_template": "file://{mirror_root}/{repo_name}.git",
            }
        ],
        "sync_on_miss": sync_on_miss,
        "sync_command": list(sync_command),
        "sync_timeout_seconds": 5,
        "git_timeout_seconds": 10,
        "reply_timeout_seconds": 20,
        "default_profile": "inmemory",
        "profiles": {"inmemory": {"kind": "inmemory"}},
        "unit": {
            "unit_name": "test-ci-checkout-serve",
            "description": "Test CI checkout serve process",
            "bus_profile": "inmemory",
            "python": "$OMNI_HOME/test/.venv/bin/python",
            "pythonpath": [],
            "restart_seconds": 5,
            "stop_timeout_seconds": 10,
            "log_file": "$OMNI_HOME/.onex_state/test-ci-checkout-serve/serve.log",
            "environment": {"PATH": "/usr/bin:/bin"},
        },
        "hosts": [
            {
                "name": host,
                "scheduler": "systemd-user",
                "mirror_root": str(mirrors),
                "serve_address": "192.0.2.1",
            }
        ],
    }


def plant_bare_mirror(tmp_path: Path) -> tuple[Path, str]:
    """A bare mirror of ``omnimarket`` holding one real commit under ``tmp_path/mirrors``: (mirrors dir, the commit's sha)."""
    import os
    import subprocess

    from omnibase_core.validators.no_unguarded_git_subprocess import (
        scrub_git_location_env,
    )

    def git(*argv: str, cwd: Path) -> str:
        return subprocess.run(
            ["git", *argv],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env=scrub_git_location_env(os.environ),
        ).stdout.strip()

    work = tmp_path / "work"
    work.mkdir(parents=True)
    git("init", "-q", cwd=work)
    (work / "hello.txt").write_text("hello\n")
    git("add", "hello.txt", cwd=work)
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        "one",
        cwd=work,
    )
    sha = git("rev-parse", "HEAD", cwd=work)
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    git(
        "clone",
        "-q",
        "--bare",
        str(work),
        str(mirrors / "omnimarket.git"),
        cwd=tmp_path,
    )
    return mirrors, sha
