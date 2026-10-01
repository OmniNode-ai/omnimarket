# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prevent validator process fan-out in pre-commit (OMN-20241).

Without require_serial, pre-commit 4.6.1 partitions filenames into batches of
max(4, ceil(N / cpu_count)) and runs up to cpu_count processes concurrently.
A 40-file commit on a 24-core Mac can spawn about ten Python interpreters per
hook, each paying the full omnibase_core import cost. Validators already accept
the whole filename list and aggregate their exit code; require_serial makes
pre-commit hand that list to one process per hook.
"""

from __future__ import annotations

from pathlib import Path
from typing import Required, TypedDict, cast

import pytest
import yaml


class HookConfig(TypedDict, total=False):
    """Hook fields relevant to the serial execution policy."""

    id: Required[str]
    language: str
    pass_filenames: bool
    require_serial: bool


class RepoConfig(TypedDict):
    """Repository and its consumer hook blocks."""

    repo: str
    hooks: list[HookConfig]


class PrecommitConfig(TypedDict):
    """Repository list in the pre-commit configuration."""

    repos: list[RepoConfig]


def hooks_missing_require_serial(config: PrecommitConfig) -> list[str]:
    """Report local and OmniNode filename hooks without explicit serial execution."""
    offenders: list[str] = []
    for repo in config["repos"]:
        if repo["repo"] != "local" and not repo["repo"].startswith(
            "https://github.com/OmniNode-ai/"
        ):
            continue
        for hook in repo["hooks"]:
            if hook.get("pass_filenames") is False or hook.get("language") == "fail":
                continue
            if hook.get("require_serial") is not True:
                offenders.append(hook["id"])
    return offenders


@pytest.mark.unit
def test_precommit_hooks_require_serial() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config = cast(
        PrecommitConfig,
        yaml.safe_load(
            (repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        ),
    )
    offenders = hooks_missing_require_serial(config)
    assert not offenders, (
        f"Hooks must declare require_serial: true (OMN-20241): {offenders}"
    )


@pytest.mark.unit
def test_reports_local_hook_missing_require_serial() -> None:
    config: PrecommitConfig = {
        "repos": [{"repo": "local", "hooks": [{"id": "local-validator"}]}]
    }
    assert hooks_missing_require_serial(config) == ["local-validator"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("repo", "hook", "expected"),
    [
        ("local", {"id": "no-filenames", "pass_filenames": False}, []),
        ("https://github.com/pre-commit/pre-commit-hooks", {"id": "third-party"}, []),
        ("local", {"id": "fail-hook", "language": "fail"}, []),
        ("local", {"id": "serial-validator", "require_serial": True}, []),
        (
            "local",
            {"id": "parallel-validator", "require_serial": False},
            ["parallel-validator"],
        ),
        (
            "https://github.com/OmniNode-ai/omnibase_core",
            {"id": "remote-validator"},
            ["remote-validator"],
        ),
        (
            "https://github.com/OmniNode-ai/omnibase_core",
            {"id": "remote-no-filenames", "pass_filenames": False},
            [],
        ),
        (
            "https://github.com/OmniNode-ai/omnibase_core",
            {"id": "remote-serial", "require_serial": True},
            [],
        ),
    ],
)
def test_serial_policy_scope(repo: str, hook: HookConfig, expected: list[str]) -> None:
    config: PrecommitConfig = {"repos": [{"repo": repo, "hooks": [hook]}]}
    assert hooks_missing_require_serial(config) == expected
