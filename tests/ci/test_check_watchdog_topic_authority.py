# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the watchdog-topic authority CI gate (OMN-12959)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci import check_watchdog_topic_authority as guard


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src" / "omnimarket" / "events").mkdir(parents=True)
    # Canonical registry (events/topics.py) is allowed to contain the literals;
    # events/watchdog.py imports them and exposes the typed authority.
    (tmp_path / "src" / "omnimarket" / "events" / "topics.py").write_text(
        'TOPIC_WORKFLOW_TIMEOUT = "onex.evt.omnimarket.workflow-timeout.v1"\n'
    )
    (tmp_path / "src" / "omnimarket" / "events" / "watchdog.py").write_text(
        "from omnimarket.events.topics import TOPIC_WORKFLOW_TIMEOUT\n"
    )
    return tmp_path


@pytest.mark.unit
def test_clean_repo_has_no_violations(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "src" / "omnimarket" / "nodes").mkdir(parents=True)
    (repo / "src" / "omnimarket" / "nodes" / "handler_ok.py").write_text(
        "from omnimarket.events.watchdog import watchdog_topic_for\n"
    )
    assert guard.scan(repo) == []


@pytest.mark.unit
def test_hardcoded_watchdog_topic_outside_registry_is_violation(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    nodes = repo / "src" / "omnimarket" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / "handler_bad.py").write_text(
        'TOPIC = "onex.evt.omnimarket.workflow-unroutable.v1"\n'
    )
    violations = guard.scan(repo)
    assert len(violations) == 1
    assert "handler_bad.py" in violations[0]


@pytest.mark.unit
def test_skip_token_exempts_line(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    nodes = repo / "src" / "omnimarket" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / "handler_fixture.py").write_text(
        'TOPIC = "onex.evt.omnimarket.workflow-stalled.v1"  # ONEX_WATCHDOG_TOPIC_OK\n'
    )
    assert guard.scan(repo) == []


@pytest.mark.unit
def test_tests_dir_is_allowlisted(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    tests = repo / "src" / "omnimarket" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_x.py").write_text('T = "onex.evt.omnimarket.workflow-timeout.v1"\n')
    assert guard.scan(repo) == []
