# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The duplicate-fix guard on a host with no PR watcher state reads the open pull requests from the gateway (OMN-22654).

Landing cause and fix workers on lab hosts pushed a fix branch and then failed at ``onex scm pr-create``, which refused
``PR-DUPLICATE-GUARD unchecked ... no PR watcher state file``: a lab host has no watcher state and the command has no
way past the guard. The refusals are recorded verbatim in ``recorded_refusals.json``.

The world is the one of ``test_scm_duplicate_guard``: a real origin, canonical clone and lane checkout, a real ledger
file. The watcher state is removed, so the open pull requests come from the gateway over the bus, answered by the real
gateway dispatcher on a recorded REST exchange. Nothing is mocked but the bus and GitHub.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.scm import scm_cli, scm_gateway_open_prs
from omnimarket.handlers.scm.scm_duplicate_guard import check
from omnimarket.handlers.scm.scm_gateway_open_prs import GatewayOpenPrFactsSource
from omnimarket.models.model_github_gateway_io import (
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
    ModelOpenPrFact,
    ModelOpenPrFactsResult,
)
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal
from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.ports.protocol_open_pr_facts_source import (
    OpenPrFactsUnreadableError,
)
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    JsonExchange,
    RecordedGatewayTransport,
    get,
    json_response,
    pull_request,
)
from tests.unit.handlers.scm.test_onex_scm_command import GATEWAY, FakeBus
from tests.unit.handlers.scm.test_scm_duplicate_guard import (
    BASELINE,
    CREATE_ARGS,
    LANE,
    NEW_BRANCH,
    TARGET,
    World,
    invoke,
)

pytestmark = pytest.mark.unit

RECORDED = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "scm_duplicate_guard_lab_hosts"
        / "recorded_refusals.json"
    ).read_text(encoding="utf-8")
)["refusals"]
PULLS = f"/repos/{TARGET}/pulls?state=open&per_page=100"
TITLES = {
    2159: "OMN-21533: follow the ci_inheritance runtime move in the protocol_binding baseline",
    2170: "feat(OMN-21600): depth metrics",
}
BRANCHES = {
    2159: "fix/omn-21533-protocol-binding-race",
    2170: "feat/omn-21600-metrics",
}


def _rest_pull(world: World, number: int) -> dict[str, object]:
    return {
        **pull_request(number, body=""),
        "title": TITLES[number],
        "head": {"ref": BRANCHES[number], "sha": world.shas[number]},
        "base": {"ref": "dev", "sha": "0" * 40},
    }


class GatewayBus(FakeBus):
    """The gateway side of the bus: the real dispatcher over one recorded REST exchange."""

    def __init__(self, exchanges: list[JsonExchange]) -> None:
        super().__init__()
        self.transport = RecordedGatewayTransport(exchanges)

    def _respond(
        self, topic: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        assert topic == GATEWAY.command
        request = ModelGithubGatewayRequest.model_validate(payload)
        response = ModelGithubGatewayResponse(
            correlation_id=request.correlation_id,
            result=dispatch(request, self.transport),
        )
        return GATEWAY.success, response.model_dump(mode="json")


class FailingGatewayBus(FakeBus):
    """A gateway that answers the request with its failure terminal."""

    def _respond(
        self, topic: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        return GATEWAY.success, {
            "outcome": "failed",
            "error": "unknown operation open_pr_facts",
        }


def _install(monkeypatch: pytest.MonkeyPatch, bus: FakeBus) -> None:
    @contextlib.asynccontextmanager
    async def opened(_: dict[str, object]) -> AsyncIterator[FakeBus]:
        yield bus

    monkeypatch.setattr(scm_cli, "_open_bus", opened)


@pytest.fixture
def lab_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """The guard's world on a host with no PR watcher state file."""
    built = World(tmp_path)
    built.state.unlink()
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(built.state))
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(built.ledger))
    monkeypatch.setenv("ONEX_SCM_CLONE_ROOT", str(built.root))
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return built


def _gateway(world: World) -> GatewayBus:
    return GatewayBus(
        [
            JsonExchange(
                get(PULLS),
                json_response([_rest_pull(world, 2170), _rest_pull(world, 2159)]),
            )
        ]
    )


def _answer(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout)


# ---- the recorded refusals


@pytest.mark.parametrize("refusal", RECORDED, ids=[f"L{r['lease']}" for r in RECORDED])
def test_recorded_refusal_reproduces_at_dev_and_passes_with_the_gateway_read(
    refusal: dict[str, Any],
    lab_host: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failing control and the fix in one test, from the verbatim refusals of the lab workers.

    Without a gateway source (the guard as it was at dev) the unrelated branch is unchecked with the recorded
    reason. With the gateway read it is clear.
    """
    lab_host.lane_branch("src/metrics.py", 15)
    recorded_reason = json.loads(refusal["stdout"])["reason"]
    assert refusal["stderr"].endswith(recorded_reason)

    # Control: the guard with no open-pull-request source, as at dev.
    code, answer = check(
        repo=TARGET,
        head=NEW_BRANCH,
        base="dev",
        title="fix(OMN-21600): another change",
        body="",
        lane=LANE,
        worktree=str(lab_host.lane),
    )
    assert code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert answer == {"state": "unchecked", "reason": recorded_reason}

    # The fix: the same host and the same branch, the open pull requests read from the gateway.
    bus = _gateway(lab_host)
    _install(monkeypatch, bus)
    result = invoke(lab_host.check_args())

    assert result.exit_code == 0, result.output
    assert _answer(result)["state"] == "clear"
    assert [envelope["payload"]["operation"] for _topic, envelope in bus.published] == [
        "open_pr_facts"
    ]
    bus.transport.assert_drained()


# ---- the same answers from the gateway as from the watcher state


def test_the_gateway_read_refuses_a_duplicate_as_the_watcher_state_does(
    lab_host: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab_host.lane_branch(BASELINE, 9)
    _install(monkeypatch, _gateway(lab_host))

    from_gateway = invoke(lab_host.check_args())

    lab_host.write_state(datetime.now(UTC))
    from_watcher = invoke(lab_host.check_args())

    assert from_gateway.exit_code == scm_cli.EXIT_DUPLICATE, from_gateway.output
    answer = _answer(from_gateway)
    assert answer["state"] == "adopt"
    assert answer["adopt_pr"] == "omnibase_core#2159"
    assert from_watcher.exit_code == from_gateway.exit_code
    assert _answer(from_watcher) == answer


def test_the_gateway_read_clears_an_unrelated_pull_request_with_a_note(
    lab_host: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab_host.lane_branch("src/metrics.py", 15)
    _install(monkeypatch, _gateway(lab_host))

    result = invoke(lab_host.check_args())

    assert result.exit_code == 0, result.output
    answer = _answer(result)
    assert answer["state"] == "clear"
    assert any("omnibase_core#2170" in note for note in answer["notes"])


def test_a_fix_marker_from_the_gateway_names_the_pull_request_that_cites_the_same_check(
    lab_host: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The body does not travel: its Fix-Check line does, and the guard folds it as it folds a watcher body."""
    lab_host.lane_branch("src/metrics.py", 15)
    pull = _rest_pull(lab_host, 2170)
    pull["body"] = "Fix-Check: Pre-commit Suite\n"
    bus = GatewayBus(
        [JsonExchange(get(PULLS), json_response([pull, _rest_pull(lab_host, 2159)]))]
    )
    _install(monkeypatch, bus)

    result = invoke(
        [
            *lab_host.check_args(),
            "--body",
            "Fix-Check: Pre-commit Suite\n",
        ]
    )

    assert result.exit_code == scm_cli.EXIT_DUPLICATE, result.output
    assert _answer(result)["adopt_pr"] == "omnibase_core#2170"


def test_a_fresh_watcher_state_is_read_and_the_gateway_is_not(
    lab_host: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab_host.lane_branch("src/metrics.py", 15)
    lab_host.write_state(datetime.now(UTC))
    bus = GatewayBus([])
    _install(monkeypatch, bus)

    result = invoke(lab_host.check_args())

    assert result.exit_code == 0, result.output
    assert bus.published == []


# ---- the guard that cannot read still refuses, and names the read it lacks


def test_a_failed_gateway_answer_leaves_the_guard_unchecked_naming_both_reads(
    lab_host: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab_host.lane_branch("src/metrics.py", 15)
    _install(monkeypatch, FailingGatewayBus())

    result = invoke(lab_host.check_args())

    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED, result.output
    reason = _answer(result)["reason"]
    assert reason.startswith("no PR watcher state file")
    assert "open_pr_facts" in reason
    assert "unknown operation open_pr_facts" in reason


def test_an_answer_that_is_cut_at_the_limit_leaves_the_guard_unchecked(
    lab_host: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A list that did not reach every open pull request cannot say that none of the rest duplicates this one."""
    lab_host.lane_branch("src/metrics.py", 15)

    class CutBus(FakeBus):
        def _respond(
            self, topic: str, payload: dict[str, Any]
        ) -> tuple[str, dict[str, Any]]:
            request = ModelGithubGatewayRequest.model_validate(payload)
            result = ModelOpenPrFactsResult(
                repo=request.repo,
                prs=[
                    ModelOpenPrFact(
                        number=1,
                        head_ref="a/b",
                        head_sha="a" * 40,
                        base_ref="dev",
                        title="t",
                        draft=False,
                    )
                ],
                total_count=412,
                truncated=True,
            )
            response = ModelGithubGatewayResponse(
                correlation_id=request.correlation_id, result=result
            )
            return GATEWAY.success, response.model_dump(mode="json")

    _install(monkeypatch, CutBus())

    result = invoke(lab_host.check_args())

    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED, result.output
    reason = _answer(result)["reason"]
    assert "cut at 1 of 412 open pull requests" in reason


class _UnopenableBus:
    """An async context manager whose entry raises ``error``."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def __aenter__(self) -> FakeBus:
        raise self._error

    async def __aexit__(self, *exc: object) -> None:
        return None


def test_a_bus_that_cannot_be_opened_leaves_the_guard_unchecked(
    lab_host: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab_host.lane_branch("src/metrics.py", 15)

    def refused(_: dict[str, object]) -> _UnopenableBus:
        return _UnopenableBus(
            LabRunBusError("node_github_repo_gateway_effect declares no event_bus")
        )

    monkeypatch.setattr(scm_cli, "_open_bus", refused)

    result = invoke(lab_host.check_args())

    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED, result.output
    reason = _answer(result)["reason"]
    assert reason.startswith("no PR watcher state file")
    assert "the bus could not be used" in reason
    assert "declares no event_bus" in reason


def test_a_gateway_that_gives_no_answer_in_time_leaves_the_guard_unchecked() -> None:
    def silent(_: dict[str, object]) -> _UnopenableBus:
        return _UnopenableBus(TimeoutError())

    source = GatewayOpenPrFactsSource(
        open_bus=silent, bus_opts={}, timeout_s=0.5, attempts=2, pause_s=0.0
    )

    with pytest.raises(OpenPrFactsUnreadableError, match=r"no answer within 0\.5s"):
        source.read(TARGET)


def test_an_answer_that_is_not_the_typed_result_is_unreadable() -> None:
    """A gateway older than this client answers the unknown operation with something that is not the result."""
    terminal = ModelScmTerminal(
        receipt=ModelScmBusReceipt(
            correlation_id=uuid.uuid4(),
            command_topic="c",
            command_envelope_id=uuid.uuid4(),
            terminal_topic="t",
            terminal_envelope_id=uuid.uuid4(),
            terminal_partition=0,
            terminal_offset="1",
        ),
        failed=False,
        payload={"result": {"operation": "pr_heads", "repo": TARGET}},
    )
    with pytest.raises(OpenPrFactsUnreadableError, match="not an open_pr_facts result"):
        scm_gateway_open_prs._typed_result(terminal)


# ---- pr-create


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def fake(*args: object, **kwargs: object) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(scm_cli, "_call_bus", fake)
    return calls


def test_pr_create_on_a_host_with_no_watcher_state_goes_on_when_the_gateway_read_is_clear(
    lab_host: World,
    sent: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab_host.lane_branch("src/metrics.py", 15)
    monkeypatch.chdir(lab_host.lane)
    _install(monkeypatch, _gateway(lab_host))

    with pytest.raises(SystemExit) as stopped:
        scm_cli.scm_group.main([*CREATE_ARGS, "--dry-run"], standalone_mode=False)

    assert stopped.value.code == 0
    assert len(sent) == 1


def test_pr_create_on_a_host_with_no_watcher_state_opens_nothing_when_the_gateway_names_a_duplicate(
    lab_host: World,
    sent: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab_host.lane_branch(BASELINE, 9)
    monkeypatch.chdir(lab_host.lane)
    _install(monkeypatch, _gateway(lab_host))

    result = invoke(CREATE_ARGS)

    assert result.exit_code == scm_cli.EXIT_DUPLICATE, result.output
    assert sent == []
    assert "omnibase_core#2159" in result.stderr


def test_pr_create_opens_nothing_when_the_gateway_read_fails(
    lab_host: World,
    sent: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab_host.lane_branch("src/metrics.py", 15)
    monkeypatch.chdir(lab_host.lane)
    _install(monkeypatch, FailingGatewayBus())

    result = invoke(CREATE_ARGS)

    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED, result.output
    assert sent == []
    assert "PR-DUPLICATE-GUARD unchecked, no pull request opened" in result.stderr
    assert "open_pr_facts" in result.stderr
