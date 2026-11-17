# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ``replaces`` entry the PR watcher state no longer holds is read from the gateway (OMN-23634).

The PR watcher keeps the open and recently merged pull requests only, so a change closed unmerged days ago has
aged out of its state and ``--replaces`` named it ``unreadable``: the live dry run of ``omn-19998-glm-lane-route``
replacing the closed draft answered ``PR-REPLACES-REFUSED reason=unreadable``. The node now asks the gateway's
``merged_pr`` read for that one pull request, and takes its state from the answer, never from the caller.

The world is the one of ``test_scm_duplicate_guard_replaces`` (a real origin, canonical clone, lane checkout and
ledger) with the closed draft removed from the watcher state; the gateway answers through the real dispatcher on a
recorded REST exchange. Nothing is mocked but the bus and GitHub.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.scm import scm_cli
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    JsonExchange,
    get,
    json_response,
    pull_request,
)
from tests.unit.handlers.scm.test_onex_scm_command import FakeBus
from tests.unit.handlers.scm.test_scm_duplicate_guard import invoke
from tests.unit.handlers.scm.test_scm_duplicate_guard_gateway_source import (
    FailingGatewayBus,
    GatewayBus,
    _UnopenableBus,
)
from tests.unit.handlers.scm.test_scm_duplicate_guard_replaces import (
    ENGINE,
    ENGINE_LINE,
    REPLACED,
    SKILL,
    SKILL_LINE,
    TARGET,
    World,
)

pytestmark = pytest.mark.unit

DRAFT_PATH = f"/repos/{TARGET}/pulls/1574"


@pytest.fixture
def aged_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """The guard's world with the closed draft gone from the watcher state, as it is days after the close."""
    built = World(tmp_path)
    state = json.loads(built.state.read_text(encoding="utf-8"))
    del state["prs"]["example_repo#1574"]
    built.state.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(built.state))
    monkeypatch.setenv("ONEX_SCM_CLONE_ROOT", str(built.root))
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return built


def _install(monkeypatch: pytest.MonkeyPatch, bus: object) -> None:
    @contextlib.asynccontextmanager
    async def opened(_: dict[str, object]) -> AsyncIterator[object]:
        yield bus

    monkeypatch.setattr(scm_cli, "_open_bus", opened)


def _draft(world: World, *, state: str, merged: bool) -> GatewayBus:
    """The gateway holding one recorded REST answer for the draft pull request."""
    rest: dict[str, Any] = {
        **pull_request(1574, body=""),
        "state": state,
        "merged": merged,
        "head": {"ref": "omn-19998-glm-engine-table", "sha": world.draft_sha},
        "base": {"ref": "main", "sha": "0" * 40},
    }
    if merged:
        rest["merged_at"] = "2026-11-09T10:00:00Z"
        rest["merge_commit_sha"] = "1" * 40
    return GatewayBus([JsonExchange(get(DRAFT_PATH), json_response(rest))])


def _refused_reason(result: Any) -> str:
    assert result.exit_code == scm_cli.EXIT_REPLACES_REFUSED, result.output
    answer = json.loads(result.stdout)
    assert answer["state"] == "replaces_refused"
    return str(answer["replaces_refusals"][0]["reason"])


def test_the_control_without_the_gateway_the_aged_out_draft_is_unreadable(
    aged_out: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured failure: the watcher state does not hold the draft and nothing else is asked."""
    monkeypatch.setattr(
        scm_cli, "_open_bus", lambda _: _UnopenableBus(LabRunBusError("no bus"))
    )

    result = invoke(aged_out.args(REPLACED))

    assert _refused_reason(result) == "unreadable"


def test_an_aged_out_closed_draft_is_read_from_the_gateway_and_its_overlaps_set_aside(
    aged_out: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _draft(aged_out, state="closed", merged=False)
    _install(monkeypatch, bus)

    result = invoke(aged_out.args(REPLACED))

    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout)
    assert answer["state"] == "clear"
    assert sorted(item["place"] for item in answer["set_aside"]) == sorted(
        [f"{ENGINE}:{ENGINE_LINE}", f"{SKILL}:{SKILL_LINE}"]
    )
    assert all(item["replaced"] == [REPLACED] for item in answer["set_aside"])
    assert [envelope["payload"]["operation"] for _, envelope in bus.published] == [
        "merged_pr"
    ]


def test_an_aged_out_entry_the_gateway_reads_as_open_is_refused_as_still_open(
    aged_out: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _draft(aged_out, state="open", merged=False))

    assert _refused_reason(invoke(aged_out.args(REPLACED))) == "still_open"


def test_an_aged_out_entry_the_gateway_reads_as_merged_is_refused_as_merged(
    aged_out: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _draft(aged_out, state="closed", merged=True))

    assert _refused_reason(invoke(aged_out.args(REPLACED))) == "merged"


def test_an_entry_the_gateway_cannot_read_is_refused_as_unreadable(
    aged_out: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FailingGatewayBus())

    assert _refused_reason(invoke(aged_out.args(REPLACED))) == "unreadable"


def test_an_entry_the_watcher_holds_is_never_asked_of_the_gateway(
    aged_out: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Put the draft back into the watcher state: the gateway bus must stay closed."""
    aged_out.write_state()
    opened: list[object] = []

    def refuse(opts: dict[str, object]) -> FakeBus:
        opened.append(opts)
        raise AssertionError("the gateway was asked for an entry the watcher holds")

    monkeypatch.setattr(scm_cli, "_open_bus", refuse)

    result = invoke(aged_out.args(REPLACED))

    assert result.exit_code == 0, result.output
    assert opened == []
