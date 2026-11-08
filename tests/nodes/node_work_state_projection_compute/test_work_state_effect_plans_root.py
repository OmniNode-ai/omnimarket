# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A plans root that is not set is carried in unread, never a crash (OMN-23013)."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import (
    build_request,
)

PLANS_ENV = "ONEX_WORK_STATE_PLANS_ROOT"


def _unread(rules, now, home: Path, plans_root: Path | None) -> list[str]:
    env = {} if plans_root is None else {PLANS_ENV: str(plans_root)}
    return list(build_request(now, home, env, None, rules).unread)


def test_an_unset_plans_root_is_reported_unread(rules, now, tmp_path) -> None:
    unread = _unread(rules, now, tmp_path, None)
    assert any(entry.startswith(f"plans:{PLANS_ENV} unset") for entry in unread)


def test_a_missing_plans_root_directory_is_reported_unread(
    rules, now, tmp_path
) -> None:
    unread = _unread(rules, now, tmp_path, tmp_path / "absent")
    assert any(entry.startswith("plans:root absent") for entry in unread)
    assert not any(entry.startswith(f"plans:{PLANS_ENV} unset") for entry in unread)


def test_a_present_plans_root_is_not_reported_unread(rules, now, tmp_path) -> None:
    """Failing control: a readable plans root leaves both plans entries out, so the two checks above can fail."""
    plans = tmp_path / "plans"
    plans.mkdir()
    unread = _unread(rules, now, tmp_path, plans)
    assert not [entry for entry in unread if entry.startswith("plans:")]
