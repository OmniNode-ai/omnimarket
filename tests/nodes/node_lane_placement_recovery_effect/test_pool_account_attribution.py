# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A limit notice is marked on one pool label only when its text names exactly one (OMN-23895).

Measured 2026-11-11: the last label in the log text won, so a notice about one account was marked on another
label and its reset stamped that label until Nov 13. A text naming several labels says nothing about which
account hit the limit, so no label is marked. A notice that names its own reset marks with ``--exact``.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from omnimarket.nodes.node_lane_placement_recovery_effect.handlers.handler_claude_account_limit import (
    HandlerClaudeAccountLimit,
)

pytestmark = pytest.mark.unit


def line(label: str) -> str:
    return f"claude-pool host=h051 tokens=5 limited=1 next_reset=none label={label}"


def handler(captured: list[str] | None = None) -> HandlerClaudeAccountLimit:
    return HandlerClaudeAccountLimit(
        {
            "CLAUDE_POOL_SH": ":",
            "CLAUDE_POOL_MODULE": "pool.module",
            "exec_argv": lambda _host, _name, script: script,
            "PROBE_TIMEOUT_S": 5,
        }
    )


def test_one_label_in_the_text_is_the_account() -> None:
    assert handler()._pool_account(line("onexbot")) == "onexbot"
    assert handler()._pool_account(f"{line('onexbot')}\n{line('onexbot')}") == "onexbot"


def test_control_two_labels_in_the_text_name_no_account() -> None:
    text = f"{line('h201-own')}\nYou've hit your weekly limit\n{line('onexbot')}\n"
    assert handler()._pool_account(text) is None


def test_no_label_names_no_account() -> None:
    assert handler()._pool_account("You've hit your weekly limit") is None


@pytest.mark.parametrize(("exact", "flag"), [(True, " --exact"), (False, "")])
def test_the_mark_command_carries_exact_only_for_a_notice_that_names_its_reset(
    exact: bool, flag: str
) -> None:
    scripts: list[str] = []

    def capture(argv: Any, timeout: float) -> subprocess.CompletedProcess[str]:
        scripts.append(argv if isinstance(argv, str) else " ".join(argv))
        return subprocess.CompletedProcess([], 0, "", "")

    rc = handler()._mark_pool_account("h051", "onexbot", 100.0, capture, exact=exact)
    assert rc == 0
    assert scripts[0].endswith(f"--seconds 101{flag}")
