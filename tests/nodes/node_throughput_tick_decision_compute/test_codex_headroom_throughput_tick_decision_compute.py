# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the tick reports Codex headroom apart from Claude headroom.

2026-10-12: the tick printed `lab-headroom:h201 | limited until ...; no dispatch` all day while h201
held a Codex login, a passing sandbox check and 11 free Codex slots. The Claude `.limited-<host>` mark
is Claude's: a Claude-limited host with free Codex slots is a MISSING line naming the Codex slots.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)

pytestmark = pytest.mark.unit

HANDLER = HandlerThroughputTickDecision()
NOW = "2026-10-12T10:00:00Z"
CLAUDE_LIMIT = {"state": "present", "until": "2026-10-12T22:01:00Z"}
# What the runner's `Reading.describe` prints for a Claude-limited host with a Codex login: the Claude
# marks empty its engines (`login=none`) and leave its slots alone.
LIMITED_CODEX = "h201:cores=32,load1=4.0,free_gb=64.0,codex=login/ok,placed=1,cap=12,slots=11,login=none"
CLAUDE_OK = "h201:cores=32,load1=4.0,free_gb=64.0,codex=login/ok,placed=1,cap=12,slots=11,login=claude"


def _host(reading: str, **overrides: Any) -> dict[str, Any]:
    return {
        "name": "h201",
        "codex_allowed": True,
        "parses": {reading: {"parsed": True}},
        **overrides,
    }


def _receipt(reading: str) -> dict[str, Any]:
    return {
        "started_at": "2026-10-12T09:59:00Z",
        "host": "h201",
        "status": "running",
        "pid_alive": True,
        "claim_host": "h201",
        "claim_run_id": "rlane-fix-a-1",
        "readings": [reading],
    }


def run(reading: str, **host: Any) -> Any:
    return HANDLER.handle(
        ModelThroughputTickRequest.model_validate(
            {
                "now": NOW,
                "launchctl": "not_loaded",
                "lab_headroom": {
                    "hosts": [_host(reading, **host)],
                    "receipts": [_receipt(reading)],
                },
            }
        )
    )


def lab_lines(result: Any) -> list[str]:
    return [line for line in result.lines if "lab-headroom" in line]


def test_claude_limited_host_with_free_codex_slots_is_a_missing_line() -> None:
    result = run(LIMITED_CODEX, limited_mark=CLAUDE_LIMIT)
    assert lab_lines(result) == [
        "MISSING lab-headroom:h201 | claude limited until 2026-10-12T22:01:00Z; "
        "codex free slots 11 (running lanes 1 of cap 12) | FIX: dispatch up to 11 Codex lanes "
        "(--model codex) to h201 through the remote-lane runner; never pin to a host without headroom"
    ]
    assert "lab-headroom:h201" in result.missing
    [codex] = result.codex_headroom
    assert (codex.host, codex.free, codex.cap, codex.running) == ("h201", 11, 12, 1)
    assert codex.claude_blocked == "claude limited until 2026-10-12T22:01:00Z"
    assert codex.refusal is None


def test_claude_auth_expired_host_with_free_codex_slots_is_a_missing_line() -> None:
    result = run(
        LIMITED_CODEX,
        auth_mark={
            "state": "present",
            "until": "2026-10-13T10:00:00Z",
            "at": "2026-10-12T04:00:00Z",
        },
    )
    [line] = lab_lines(result)
    assert line.startswith(
        "MISSING lab-headroom:h201 | claude auth expired since 2026-10-12T04:00:00Z; codex free slots 11"
    )


def test_a_codex_sandbox_word_of_fail_does_not_suppress_codex_headroom() -> None:
    # The `.codex-sandbox-<host>` marks gate landing workers only; the lane's run script probes its own sandbox.
    reading = LIMITED_CODEX.replace("codex=login/ok", "codex=login/fail")
    result = run(reading, limited_mark=CLAUDE_LIMIT)
    assert "codex free slots 11" in lab_lines(result)[0]
    assert lab_lines(result)[0].startswith("MISSING lab-headroom:h201 |")


@pytest.mark.parametrize(
    ("reading", "host", "why"),
    [
        (LIMITED_CODEX.replace("login/ok", "nologin/ok"), {}, "no Codex login"),
        (
            LIMITED_CODEX.replace(
                "login=none", "login=none,CODEX-LIMITED-UNTIL=2026-10-12T12:00:00Z"
            ),
            {},
            "Codex usage limit until 2026-10-12T12:00:00Z",
        ),
        (LIMITED_CODEX.replace("codex=login/ok,", ""), {}, "Codex login unread"),
        (LIMITED_CODEX.replace("slots=11", "slots=0"), {}, "no free Codex slots"),
        (LIMITED_CODEX, {"codex_allowed": False}, None),
    ],
    ids=[
        "no-login",
        "codex-limited",
        "login-unread",
        "no-slots",
        "host-table-no-codex",
    ],
)
def test_a_host_limited_for_both_engines_still_says_no_dispatch(
    reading: str, host: dict[str, Any], why: str | None
) -> None:
    result = run(reading, limited_mark=CLAUDE_LIMIT, **host)
    [line] = lab_lines(result)
    assert line.startswith(
        "NOTE lab-headroom:h201 | limited until 2026-10-12T22:01:00Z; no dispatch"
    )
    assert (why is None) or line.endswith(f"(codex: {why})")
    assert "lab-headroom:h201" not in result.missing
    assert all(c.free == 0 for c in result.codex_headroom)


def test_a_claude_limited_host_with_an_unread_codex_headroom_says_so() -> None:
    result = HANDLER.handle(
        ModelThroughputTickRequest.model_validate(
            {
                "now": NOW,
                "launchctl": "not_loaded",
                "lab_headroom": {
                    "hosts": [_host(LIMITED_CODEX, limited_mark=CLAUDE_LIMIT)],
                    "receipts": [],
                },
            }
        )
    )
    [line] = lab_lines(result)
    assert line.startswith(
        "NOTE lab-headroom:h201 | limited until 2026-10-12T22:01:00Z; no dispatch"
    )
    assert line.endswith("(codex: no placement reading in the last 15 min)")


def test_a_host_with_claude_headroom_keeps_todays_line_and_adds_only_the_typed_reading() -> (
    None
):
    result = run(CLAUDE_OK)
    assert lab_lines(result) == [
        "MISSING lab-headroom:h201 | running lanes 1 of cap 12, free slots 11 | FIX: dispatch up to 11 lanes "
        "of the session's pillar work to h201 through the remote-lane runner; never pin to a host without headroom"
    ]
    [codex] = result.codex_headroom
    assert (codex.host, codex.free, codex.claude_blocked) == ("h201", 11, None)


def test_without_codex_facts_the_result_dump_is_what_it_was() -> None:
    result = run(CLAUDE_OK, codex_allowed=False)
    assert result.codex_headroom == []
    assert "codex_headroom" not in result.model_dump()
