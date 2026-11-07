# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22924 AC3: the pushed status file, the emissions and the failure text never hold a credential.

The scan reads the content as written to the remote, and the failure text of a push whose remote carries a credential.
Named injected break: ``break_leak_token`` (a status writer that adds a token field) is shown caught by the scan.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.models.floor_17a import ModelCanaryStatus
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.canary_publisher import (
    GitCanaryPublisher,
)
from tests.unit.nodes.node_factory_floor_publish_effect.canary_support import (
    bare_remote,
    canary_block,
    git,
    read_pushed_text,
    run_canary_step,
    served_ages,
)

pytestmark = pytest.mark.unit

CREDENTIAL_SHAPES = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"(?i)\b(token|secret|password|passwd|api[_-]?key|authorization)\b"),
)
PLANTED = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"


def credential_findings(content: str) -> list[str]:
    return [p.pattern for p in CREDENTIAL_SHAPES if p.search(content)]


def test_golden_the_pushed_status_holds_no_credential(tmp_path: Path) -> None:
    remote = bare_remote(tmp_path)
    result, sink = run_canary_step(
        tmp_path, canary_block(str(remote)), served=served_ages(5)
    )
    assert result.ok
    content = read_pushed_text(remote)
    assert content is not None
    assert credential_findings(content) == []
    emitted = " ".join(e.model_dump_json() for e in sink.events)
    assert credential_findings(emitted) == []


def test_error_a_failed_push_on_a_credentialed_remote_leaks_no_credential(
    tmp_path: Path,
) -> None:
    """A remote URL with a user and token embedded fails to push; git's own words and every record of the failure stay clean."""
    credentialed = f"https://x-access-token:{PLANTED}@127.0.0.1:9/org/floor-canary.git"
    result, sink = run_canary_step(
        tmp_path, canary_block(credentialed), served=served_ages(5)
    )
    assert not result.ok
    records = " ".join(
        [result.failure_detail or "", result.canary or ""]
        + [e.model_dump_json() for e in sink.events]
    )
    assert PLANTED not in records
    assert credential_findings(records) == []


def test_error_git_words_that_echo_the_remote_credential_are_scrubbed_from_the_failure(
    tmp_path: Path,
) -> None:
    """Older git prints the remote URL with its userinfo in a push failure; the typed failure and its emissions must not carry it."""
    credentialed = (
        f"https://x-access-token:{PLANTED}@example.invalid/org/floor-canary.git"
    )
    canary = canary_block(credentialed)

    def leaky_git(argv: Sequence[str], cwd: Path, timeout_s: int) -> tuple[int, str]:
        if "push" in argv:
            return (
                128,
                f"fatal: unable to access '{credentialed}': Could not resolve host",
            )
        return 0, ""

    publisher = GitCanaryPublisher(canary, tmp_path / "clone", leaky_git).publish
    result, sink = run_canary_step(
        tmp_path, canary, served=served_ages(5), publisher=publisher
    )
    assert not result.ok
    records = " ".join(
        [result.failure_detail or "", result.canary or ""]
        + [e.model_dump_json() for e in sink.events]
    )
    assert "git push failed" in records
    assert PLANTED not in records
    assert credential_findings(records) == []


def test_control_break_leak_token_is_caught_by_the_scan(tmp_path: Path) -> None:
    """The failing control of AC3: a status writer that adds a token field is flagged by the scan of the written content."""
    remote = bare_remote(tmp_path)
    canary = canary_block(str(remote))

    def break_leak_token(status: ModelCanaryStatus) -> None:
        clone = tmp_path / "leak-clone"
        identity = ("-c", "user.name=x", "-c", "user.email=x@example.invalid")
        assert git("clone", "-q", str(remote), str(clone)).returncode == 0
        (clone / canary.file_name).write_text(
            status.model_dump_json()[:-1] + f', "token": "{PLANTED}"' + "}",
            encoding="utf-8",
        )
        assert git("-C", str(clone), "add", "--", canary.file_name).returncode == 0
        committed = git("-C", str(clone), *identity, "commit", "-q", "-m", "leak")
        assert committed.returncode == 0
        target = f"HEAD:refs/heads/{canary.branch}"
        assert git("-C", str(clone), "push", "-q", "origin", target).returncode == 0

    run_canary_step(tmp_path, canary, served=served_ages(5), publisher=break_leak_token)
    content = read_pushed_text(remote)
    assert content is not None
    assert credential_findings(content) != []
