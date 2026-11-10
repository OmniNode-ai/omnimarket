# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22630: a diff that cannot be read counts as a change that runs.

``_changed_paths`` answers ``src/<unreadable diff>`` when the base cannot be
resolved or the log cannot be read, so an unknown change is charged as runtime
code and a ticket whose contract carries no integration proof is refused. Before
this test nothing failed if that answer became an empty tuple: the integration
proof rule then saw no runtime path and let an unreadable push through.

The failing control is the handler with that one return replaced by ``()``,
shown in the pull request.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers import (
    handler_ticket_contract_prerequisite as boundary,
)

pytestmark = pytest.mark.unit

TICKET = "OMN-22630"
UNREADABLE = ("src/<unreadable diff>",)
CONTRACT = (
    f"ticket_id: {TICKET}\n"
    "requirements:\n"
    "  - id: req\n"
    "    statement: s\n"
    "    acceptance:\n"
    "      - id: AC1\n"
    "        statement: 'AC1: x -- falsifier: uv run pytest tests/test_x.py -q'\n"
    "dod_evidence:\n"
    "  - id: dod-unit\n"
    "    description: unit tests\n"
    "    source: manual\n"
    "    binds_ac: [AC1]\n"
    "    checks:\n"
    "      - check_type: test_passes\n"
    "        check_value: uv run pytest tests/test_x.py -q\n"
)


def _git(repo: Path, *args: str) -> str:
    identity = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "lane@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "lane@example.com",
    }
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env({**os.environ, **identity}),
    ).stdout.strip()


@pytest.fixture
def pushed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A pushed commit that adds its contract and one source file, with no base ref to read."""
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    _git(tmp_path, "init", "-q", "-b", "dev")
    (tmp_path / "README.md").write_text("base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / f"{TICKET}.yaml").write_text(CONTRACT)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "own.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", f"feat({TICKET}): change")
    monkeypatch.chdir(tmp_path)
    return _git(tmp_path, "rev-parse", "HEAD")


def test_an_unreadable_base_names_the_unreadable_diff_marker(pushed: str) -> None:
    assert boundary._changed_paths(pushed, "no-such-base") == UNREADABLE


def test_a_readable_base_names_the_real_paths(pushed: str) -> None:
    assert boundary._changed_paths(pushed, f"{pushed}~1") == (
        f"contracts/{TICKET}.yaml",
        "src/own.py",
    )


def test_an_unreadable_diff_is_refused_as_an_unproven_runtime_change(
    pushed: str,
) -> None:
    failures = boundary.check(pushed, "no-such-base", f"feat({TICKET}): change")
    assert len(failures) == 1
    assert TICKET in failures[0]
    assert "needs an integration proof" in failures[0]
    assert "src/<unreadable diff>" in failures[0]


def test_a_readable_base_with_the_same_push_is_refused_for_its_real_path(
    pushed: str,
) -> None:
    """Positive control: the refusal is about the path, not about the missing base."""
    failures = boundary.check(pushed, f"{pushed}~1", f"feat({TICKET}): change")
    assert len(failures) == 1
    assert "src/own.py" in failures[0]
    assert "src/<unreadable diff>" not in failures[0]
