# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C30 grading logic, pinned offline (ported from the cloud repository's OMN-19989 tests, OMN-19966).

NO NETWORK and no process: a guard replaces the one runner the probes use for ``onex dashboard`` and fails any test
that reached it, and every test is offline and takes no skip.
"""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_c30_claude_code_plugin_probe_effect.handlers import (
    probe_base as base,
)
from omnimarket.nodes.node_c30_claude_code_plugin_probe_effect.handlers import (
    probe_c30 as c30,
)
from omnimarket.nodes.node_c30_claude_code_plugin_probe_effect.handlers.probe_base import (
    VERDICT_FAIL,
    VERDICT_PRECONDITION_ABSENT,
    Precondition,
)

pytestmark = pytest.mark.unit

PRESENT = Precondition(name="p", present=True, detail="d", owner="OMN-1")
ABSENT = Precondition(name="q", present=False, detail="d", owner="OMN-2")


@pytest.fixture(autouse=True)
def no_dashboard_starts(monkeypatch: pytest.MonkeyPatch):
    """Fail any test that reached `onex dashboard` through the probes' runner (OMN-19984 AC6)."""
    started: list[list[str]] = []
    real = base._run

    def refuse(argv, *args, **kwargs):
        if list(argv[:2]) == ["onex", "dashboard"] and "--help" not in argv:
            started.append(list(argv))
            return 127, "", "unit tests never start a dashboard"
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(base, "_run", refuse)
    yield started
    assert not started, f"a unit test reached the dashboard runner: {started}"


def test_the_process_guard_sees_a_real_start_attempt(no_dashboard_starts) -> None:
    """Positive control for the guard: the dashboard starter does reach it."""
    with base.running_dashboard({}) as start:
        assert start.base_url is None
        assert "onex dashboard start --json" in start.detail
    assert no_dashboard_starts[0] == ["onex", "dashboard", "start", "--json"]
    no_dashboard_starts.clear()


def test_c30_keeps_the_one_hour_bound_from_the_criterion() -> None:
    assert c30.BUDGET_SECONDS == 3600


def test_c30_checks_the_token_variable_by_presence_and_never_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that echoed the credential it checks for would be the leak."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sensitive-value-must-not-appear")
    # `onex` is deliberately left absent so the verdict is PRECONDITION-ABSENT
    # and the whole record renders. The token precondition still reports present,
    # which is the one whose detail must not carry the value.
    monkeypatch.setattr(c30, "onex_cli", lambda: ABSENT)
    result = c30.run()
    token = next(p for p in result.preconditions if "CLAUDE_CODE_OAUTH_TOKEN" in p.name)
    assert token.present, "the variable was set, so the precondition must read present"
    blob = json.dumps(result.record()) + result.render()
    assert "sensitive-value-must-not-appear" not in blob


def test_c30_reports_precondition_absent_when_the_token_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(c30, "onex_cli", lambda: PRESENT)
    monkeypatch.setattr(c30, "claude_cli", lambda: PRESENT)
    result = c30.run()
    assert result.verdict() == VERDICT_PRECONDITION_ABSENT
    assert [p.name for p in result.missing] == ["$CLAUDE_CODE_OAUTH_TOKEN"]


def test_c30_delegation_clauses_cannot_pass_without_the_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a complete delegation walk cannot publish a green dashboard claim."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    monkeypatch.setattr(c30, "onex_cli", lambda: PRESENT)
    monkeypatch.setattr(c30, "claude_cli", lambda: PRESENT)
    correlation_id = "12345678-1234-4234-8234-123456789abc"

    def fake_walk(
        profile_kind, marketplace_source, plugin_id, scratch, nonce, **kwargs
    ):
        if profile_kind != "published":
            return c30.WalkResult(install_ok=True, execution_ok=True, claude_rc=0)
        return c30.WalkResult(
            nonce=nonce,
            install_ok=True,
            skills=["delegate"],
            execution_ok=True,
            delegation=c30.DelegationRun(True, correlation_id, "success"),
            receipt={"correlation_id": correlation_id, "prompt_text": nonce},
        )

    monkeypatch.setattr(c30, "walk", fake_walk)
    result = c30.run(
        dashboard=lambda *_a: pytest.fail(
            "walk is replaced; nothing reads the dashboard"
        )
    )
    assert result.verdict() == VERDICT_FAIL
    assert [c.ok for c in result.clauses] == [True, True, True, True, False]
    assert [c.name for c in result.clauses[:4]] == [
        "the published plugin installs into a scratch profile and ships the delegate skill",
        "/onex:delegate run from that profile ran onex delegate and returned a correlation id",
        "that run's receipt is in the store under its own correlation id",
        "negative control: a plugin carrying no skill fails the positive leg",
    ]
    assert result.clauses[4].name == c30.DASHBOARD_CLAUSE
    assert "dashboard not read" in result.clauses[4].detail


def test_c30_runs_offline_and_never_reports_a_pass_it_did_not_earn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    result = c30.run(dashboard=lambda *_a: pytest.fail("offline: no dashboard read"))
    assert result.verdict() in {VERDICT_FAIL, VERDICT_PRECONDITION_ABSENT}
