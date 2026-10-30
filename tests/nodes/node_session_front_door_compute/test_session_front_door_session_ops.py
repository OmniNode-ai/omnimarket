# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The session file and row operations, as the way-of-working session script had them.

``init`` accepts every host mode including ``bus-member`` and records the session's principal and
profile; ``show``, ``lanes``, ``boundary``, ``name`` and the CLAIM, STATUS and TERMINAL rows keep
their shapes and refusals.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_session_front_door_compute.models import HOST_MODES
from tests.session_front_door_machine import MEMBER_A, OPERATOR, Machine

pytestmark = pytest.mark.unit

TS = "2026-10-29T10:00:00Z"
DIGEST = """catchup since=2026-10-29T00:00:00Z
1. Open CLAIMs in the window
   1: lane-a
   2: sess-front
2. HOLDs
"""


def test_host_modes_include_bus_member() -> None:
    assert HOST_MODES == ("coordination", "bus-member", "detached", "unknown")


def test_name_shape(machine: Machine) -> None:
    result = machine.run("name", MEMBER_A, purpose="orchestrate")
    assert result.status == "ok"
    assert re.fullmatch(r"orchestrate-[0-9a-f]{4}", result.lane or "")
    assert machine.run("name", MEMBER_A, purpose="Bad Purpose").status == "refused"


@pytest.mark.parametrize("mode", HOST_MODES)
def test_init_writes_every_host_mode_and_the_principal(
    machine: Machine, mode: str
) -> None:
    result = machine.run(
        "init",
        MEMBER_A,
        lane="sess-x",
        claim_ts=TS,
        ticket="OMN-9000001",
        host_mode=mode,
    )
    assert result.status == "ok", result.message
    env = machine.session_env("sess-x")
    assert env["HOST_MODE"] == mode
    assert env["SESSION_PRINCIPAL"] == "fixture-member-a-dev"
    assert env["SESSION_PROFILE"] == "team"
    assert env["OMNI_HOME"] == str(machine.omni_home.resolve())
    assert result.session_env is not None
    assert result.session_env.endswith("sess-x/session.env")


def test_init_refuses_an_unknown_host_mode(machine: Machine) -> None:
    with pytest.raises(ValidationError, match="host_mode"):
        machine.request(
            "init",
            MEMBER_A,
            lane="sess-x",
            claim_ts=TS,
            ticket="OMN-1",
            host_mode="attached",
        )


def test_init_refuses_overwrite_and_dry_run_writes_nothing(machine: Machine) -> None:
    fields = {
        "lane": "sess-x",
        "claim_ts": TS,
        "ticket": "OMN-9000001",
        "host_mode": "bus-member",
    }
    dry = machine.run("init", MEMBER_A, dry_run=True, **fields)
    assert dry.status == "ok"
    assert any("SESSION_PRINCIPAL=fixture-member-a-dev" in line for line in dry.lines)
    assert not (machine.omni_home / ".claude_scratch" / "sessions" / "sess-x").exists()
    assert machine.run("init", MEMBER_A, **fields).status == "ok"
    again = machine.run("init", MEMBER_A, **fields)
    assert again.status == "refused"
    assert again.exit_code == 3


@pytest.mark.parametrize(
    ("field", "value"), [("ticket", "9000001"), ("claim_ts", "yesterday")]
)
def test_init_refuses_bad_ticket_and_stamp(
    machine: Machine, field: str, value: str
) -> None:
    fields = {
        "lane": "sess-x",
        "claim_ts": TS,
        "ticket": "OMN-9000001",
        "host_mode": "detached",
    }
    fields[field] = value
    assert machine.run("init", MEMBER_A, **fields).status == "refused"


def test_operator_init_records_the_operator_principal(machine: Machine) -> None:
    machine.run(
        "init",
        OPERATOR,
        lane="sess-o",
        claim_ts=TS,
        ticket="OMN-9000001",
        host_mode="coordination",
    )
    env = machine.session_env("sess-o")
    assert env["SESSION_PRINCIPAL"] == "fixture-lab-login"
    assert env["SESSION_PROFILE"] == "operator"


def test_show_lanes_and_boundary(machine: Machine) -> None:
    machine.run(
        "init",
        MEMBER_A,
        lane="sess-front",
        claim_ts=TS,
        ticket="OMN-9000001",
        host_mode="bus-member",
    )
    lanes = (
        machine.omni_home / ".claude_scratch" / "sessions" / "sess-front" / "lanes.txt"
    )
    lanes.write_text(lanes.read_text() + "lane-a dispatched OMN-1\nlane-b handed-off\n")
    shown = machine.run("show", MEMBER_A, lane="sess-front")
    assert "HOST_MODE=bus-member" in shown.lines
    assert machine.run("lanes", MEMBER_A, lane="sess-front").lines == (
        "lane-a",
        "lane-b",
    )
    assert machine.run(
        "lanes", MEMBER_A, lane="sess-front", state="handed-off"
    ).lines == ("lane-b",)
    boundary = machine.run("boundary", MEMBER_A, lane="sess-front", digest=DIGEST)
    assert boundary.lines == (
        "OPEN lane-a (lanes.txt state=dispatched)",
        "NOT-OPEN lane-b (lanes.txt state=handed-off)",
        "OPEN sess-front (the session CLAIM itself; closed by the session TERMINAL)",
    )
    truncated = DIGEST.replace(
        "2. HOLDs", "   ... 4 older rows in this section not shown\n2. HOLDs"
    )
    assert (
        machine.run("boundary", MEMBER_A, lane="sess-front", digest=truncated).status
        == "refused"
    )
    assert (
        machine.run("boundary", MEMBER_A, lane="sess-front", digest="nothing").status
        == "refused"
    )


def test_claim_status_and_terminal_rows(machine: Machine) -> None:
    claim = machine.run(
        "claim_row",
        MEMBER_A,
        row={
            "ts": TS,
            "lane": "sess-front",
            "ticket": "OMN-9000001",
            "actor": "claude:fixture:foreground",
            "model": "fixture-model",
            "scope": "fixture",
            "est_hours": 2,
            "displaces": "nothing queued",
            "text": "Orchestration session.",
        },
    )
    assert claim.lines == (
        f"{TS} | CLAIM | lane=sess-front | actor=claude:fixture:foreground | model=fixture-model"
        " | ticket=OMN-9000001 | scope=fixture | worktree=none"
        " | est ~2 lane-hours; displaces nothing queued; (OMN-9000001) | Orchestration session.",
    )
    status = machine.run(
        "status_row",
        MEMBER_A,
        row={"ts": TS, "lane": "sess-front", "t0": TS, "text": "resumed"},
    )
    assert status.lines == (f"{TS} | STATUS | lane=sess-front | T0={TS} | resumed",)
    terminal = machine.run(
        "terminal_row",
        MEMBER_A,
        row={
            "ts": TS,
            "lane": "sess-front",
            "closes_claim": TS,
            "outcome": "done",
            "friction": "none",
            "text": "end",
        },
    )
    assert terminal.lines == (
        f"{TS} | TERMINAL | lane=sess-front | closes-CLAIM={TS} | outcome=done | friction=none | end",
    )
    refused = machine.run(
        "terminal_row", MEMBER_A, row={"ts": TS, "lane": "sess-front", "text": "end"}
    )
    assert refused.status == "refused"
    placeholder = machine.run(
        "claim_row",
        MEMBER_A,
        row={
            "ts": TS,
            "lane": "l",
            "ticket": "OMN-1",
            "actor": "tbd",
            "est_hours": 1,
            "displaces": "x",
            "text": "t",
        },
    )
    assert placeholder.status == "refused"
