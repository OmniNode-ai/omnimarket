# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22924 AC1 and AC2: the publish node's canary step pushes the three-field status file to the canary-status branch, and a failed push is a typed failure.

Golden chain: the served page says its age and the alarm heartbeat, the step writes the status file and pushes it, and a reader of the
remote branch reads the three fields back. Error chain: the push fails and the run, the emission chain and the result carry the typed code.
Named injected breaks (each shown red in the control tests): ``break_no_push`` (a step that returns without pushing) and
``break_swallow_push_error`` (a step that hides a failed push).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from omnimarket.models.floor_17a import ModelCanaryStatus
from tests.unit.nodes.node_factory_floor_publish_effect.canary_support import (
    NOW,
    bare_remote,
    canary_block,
    commit_count,
    read_pushed,
    run_canary_step,
    served_ages,
)

pytestmark = pytest.mark.unit

THREE_FIELDS = ("floor_generated_at", "alarm_heartbeat_at", "published_at")


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def assert_three_fields_pushed(
    remote: Path, floor_at: datetime, beat_at: datetime
) -> None:
    pushed = read_pushed(remote)
    assert pushed is not None, "nothing is on the canary-status branch"
    for field in THREE_FIELDS:
        assert pushed[field] is not None, f"{field} is missing from the pushed status"
    assert datetime.fromisoformat(pushed["floor_generated_at"]) == floor_at
    assert datetime.fromisoformat(pushed["alarm_heartbeat_at"]) == beat_at
    assert datetime.fromisoformat(pushed["published_at"]) == NOW


def test_golden_the_status_file_is_pushed_and_read_back_with_the_three_fields(
    tmp_path: Path,
) -> None:
    remote = bare_remote(tmp_path)
    floor_at, beat_at = served_ages(5)
    result, sink = run_canary_step(
        tmp_path, canary_block(str(remote)), served=(floor_at, beat_at)
    )
    assert result.ok
    assert result.canary == "published"
    assert_three_fields_pushed(remote, floor_at, beat_at)
    chain = [e for e in sink.events if e.node.endswith("publish_canary")]
    assert [e.phase for e in chain] == ["start", "success"]


def test_golden_a_second_publication_replaces_the_commit_so_the_branch_does_not_grow(
    tmp_path: Path,
) -> None:
    remote = bare_remote(tmp_path)
    canary = canary_block(str(remote))
    first = served_ages(7)
    second = served_ages(2)
    run_canary_step(tmp_path / "one", canary, served=first)
    run_canary_step(tmp_path / "two", canary, served=second)
    assert commit_count(remote) == 1
    assert_three_fields_pushed(remote, *second)


def test_control_break_no_push_is_caught_by_the_read_back(tmp_path: Path) -> None:
    """The failing control of AC1: a step that returns without pushing leaves the branch empty and the read-back goes red."""
    remote = bare_remote(tmp_path)
    floor_at, beat_at = served_ages(5)

    def break_no_push(status: ModelCanaryStatus) -> None:
        return None

    result, _ = run_canary_step(
        tmp_path,
        canary_block(str(remote)),
        served=(floor_at, beat_at),
        publisher=break_no_push,
    )
    assert result.canary == "published"  # the step itself reports success ...
    with pytest.raises(AssertionError, match="nothing is on the canary-status branch"):
        assert_three_fields_pushed(
            remote, floor_at, beat_at
        )  # ... and only the read-back is red


def test_error_failed_push_is_a_typed_failure_and_never_a_silent_skip(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "no-such-remote.git"
    result, sink = run_canary_step(
        tmp_path, canary_block(str(missing)), served=served_ages(5)
    )
    assert not result.ok
    assert result.canary == "failed_canary_push_failed"
    assert result.failure_code == "canary_failed_canary_push_failed"
    assert "was not published" in result.failure_detail
    chain = [e for e in sink.events if e.node.endswith("publish_canary")]
    assert [(e.phase, e.failure_code) for e in chain] == [
        ("start", None),
        ("failure", "canary_push_failed"),
    ]
    assert "git push failed" in (chain[-1].failure_detail or "")
    assert read_pushed(missing) is None


def test_error_failed_push_with_an_unreadable_served_page_is_also_typed(
    tmp_path: Path,
) -> None:
    remote = bare_remote(tmp_path)
    result, _ = run_canary_step(tmp_path, canary_block(str(remote)), served=None)
    assert not result.ok
    assert result.failure_code == "canary_unpublished_served_page_unread"
    assert read_pushed(remote) is None


def test_control_failed_push_break_swallow_push_error_is_caught(tmp_path: Path) -> None:
    """The failing control of AC2: a step that hides a failed push reports ok, and the typed-failure assertions go red."""
    missing = tmp_path / "no-such-remote.git"

    def break_swallow_push_error(status: ModelCanaryStatus) -> None:
        return None

    result, sink = run_canary_step(
        tmp_path,
        canary_block(str(missing)),
        served=served_ages(5),
        publisher=break_swallow_push_error,
    )
    assert result.ok  # the swallowed push looks like a success ...
    chain = [e for e in sink.events if e.node.endswith("publish_canary")]
    assert [e.phase for e in chain] == [
        "start",
        "success",
    ]  # ... and the typed failure never appears
    assert result.canary != "failed_canary_push_failed"
