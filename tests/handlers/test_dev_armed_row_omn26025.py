# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev-armed row at 29 and 31 minutes (OMN-26025)."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.dev_armed_row import (
    ArmedState,
    dev_armed_row,
    read_armed_state,
)

pytestmark = pytest.mark.unit

TAGGED = datetime(2026, 11, 19, 2, 0, 0, tzinfo=UTC)


def _state(dev: str = "0.4.321", tag: str = "v0.4.321") -> ArmedState:
    return ArmedState(dev, tag, TAGGED)


def test_dev_armed_row_is_raised_at_31_minutes_with_an_owner() -> None:
    row = dev_armed_row(
        _state(), now=TAGGED + timedelta(minutes=31), owner="release-owner"
    )
    assert row is not None
    assert row.count("\n") == 0
    assert " | STATUS | " in row
    assert "owner=release-owner" in row
    assert "armed_minutes=31" in row
    assert "latest_tag=v0.4.321" in row


@pytest.mark.parametrize("minutes", [0, 29, 30])
def test_dev_armed_row_is_not_raised_at_or_under_30_minutes(minutes: int) -> None:
    assert (
        dev_armed_row(_state(), now=TAGGED + timedelta(minutes=minutes), owner="o")
        is None
    )


def test_dev_armed_row_is_not_raised_once_dev_is_ahead_of_the_tag() -> None:
    late = TAGGED + timedelta(hours=10)
    assert dev_armed_row(_state(dev="0.4.322"), now=late, owner="o") is None


def test_dev_armed_row_refuses_an_empty_owner() -> None:
    with pytest.raises(ValueError, match="owner"):
        dev_armed_row(_state(), now=TAGGED + timedelta(hours=1), owner=" ")


def test_dev_armed_row_reads_a_clone_whose_dev_equals_the_highest_tag(
    tmp_path: Path,
) -> None:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            env={
                **scrub_git_location_env(os.environ),
                "GIT_COMMITTER_DATE": TAGGED.isoformat(),
                "GIT_AUTHOR_DATE": TAGGED.isoformat(),
            },
        )

    git("init", "-q", "-b", "dev")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.4.321"\n'
    )
    git("add", "-A")
    git("commit", "-q", "-m", "release")
    git("tag", "-a", "v0.4.320", "-m", "older")
    git("tag", "-a", "v0.4.321", "-m", "release")
    git("update-ref", "refs/remotes/origin/dev", "HEAD")

    state = read_armed_state(tmp_path)
    assert (state.dev_version, state.highest_tag, state.tag_time) == (
        "0.4.321",
        "v0.4.321",
        TAGGED,
    )
    assert dev_armed_row(state, now=TAGGED + timedelta(minutes=29), owner="o") is None
    assert (
        dev_armed_row(state, now=TAGGED + timedelta(minutes=31), owner="o") is not None
    )
