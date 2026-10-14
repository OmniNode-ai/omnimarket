# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A release cut's diff cannot carry a contract, so the prerequisite exempts it.

The receipt gate admits a bot release cut only while its diff against the merge
base is the root CHANGELOG.md plus optional pyproject.toml and uv.lock. A
contracts/OMN-*.yaml file added to the cut leaves that allowlist and fails the
gate, while this prerequisite refused the cut for carrying no contract. Each
test drives a real git repository, so the exemption reads the merge-base diff.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_dod_verify.services.contract_prerequisite import (
    missing_contract_message,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers import (
    handler_ticket_contract_prerequisite as push,
)

pytestmark = pytest.mark.unit
TICKET = "OMN-18595"
TITLE = f"chore: release omnimarket 0.4.313 ({TICKET})"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def _cut(repo: Path, files: dict[str, str]) -> str:
    """A repo whose dev branch holds pyproject.toml, and a branch adding `files`."""
    _git(repo, "init", "-q", "-b", "dev")
    (repo / "pyproject.toml").write_text("[project]\nversion = '0.4.313'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "switch", "-q", "-c", "release")
    for name, body in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    _git(repo, "add", "-f", "-A")
    _git(repo, "commit", "-q", "-m", TITLE)
    return _git(repo, "rev-parse", "HEAD")


def _check(
    monkeypatch: pytest.MonkeyPatch, repo: Path, head: str, base: str = "dev"
) -> tuple[str, ...]:
    # The module's own git helper runs in the working directory, and a hook's
    # GIT_* location variables would override it.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(repo)
    return push.check(head, base, TITLE)


def test_changelog_only_release_cut_needs_no_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _cut(tmp_path, {"CHANGELOG.md": "## v0.4.313 (2026-10-13)\n"})
    assert _check(monkeypatch, tmp_path, head) == ()


def test_release_cut_with_pins_needs_no_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _cut(
        tmp_path,
        {"CHANGELOG.md": "## v0.4.313 (2026-10-13)\n", "uv.lock": "version = 1\n"},
    )
    assert _check(monkeypatch, tmp_path, head) == ()


def test_changelog_plus_source_change_still_needs_a_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _cut(
        tmp_path,
        {"CHANGELOG.md": "## v0.4.313 (2026-10-13)\n", "src/pkg/mod.py": "x = 1\n"},
    )
    assert _check(monkeypatch, tmp_path, head) == (missing_contract_message(TICKET),)


def test_source_change_without_changelog_still_needs_a_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _cut(tmp_path, {"src/pkg/mod.py": "x = 1\n"})
    assert _check(monkeypatch, tmp_path, head) == (missing_contract_message(TICKET),)


def test_pins_without_changelog_still_need_a_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _cut(tmp_path, {"uv.lock": "version = 1\n"})
    assert _check(monkeypatch, tmp_path, head) == (missing_contract_message(TICKET),)


def test_nested_changelog_is_not_a_release_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _cut(tmp_path, {"docs/CHANGELOG.md": "## v0.4.313 (2026-10-13)\n"})
    assert _check(monkeypatch, tmp_path, head) == (missing_contract_message(TICKET),)


def test_unreadable_base_keeps_the_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _cut(tmp_path, {"CHANGELOG.md": "## v0.4.313 (2026-10-13)\n"})
    assert _check(monkeypatch, tmp_path, head, "no-such-base") == (
        missing_contract_message(TICKET),
    )
