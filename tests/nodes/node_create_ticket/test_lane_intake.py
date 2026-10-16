# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane intake for the nodes that open Linear items (OMN-17942).

Every item opened from a lane or node context is labelled, stamped on its first
line with its lane, refused without a lane identity, and counted against a
per-day budget. The vocabulary and the budget belong to the omniclaude policy;
these tests pin what the nodes do with them, with a stand-in guard script for the
real one (whose own rules are tested in omniclaude).

Every refusal case has an admitted twin beside it.
"""

from __future__ import annotations

import json
import stat
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.handler_lane_intake import (
    LaneIntakeGate,
    LaneIntakeRefusedError,
    ModelLaneIntakePolicy,
    admit_lane_intake,
    declared_lane,
    read_lane_intake_policy,
    resolve_label_id,
    stamp_description,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    InstalledPluginTicketGuard,
    LinearTicketHttpGateway,
    ModelCreateTicketRequest,
    ModelTicketGuardDecision,
)

pytestmark = pytest.mark.unit

_POLICY = ModelLaneIntakePolicy(label="the-label", stamp_prefix="opened-by:")


class _Guard:
    def __init__(self) -> None:
        self.inputs: list[dict[str, object]] = []

    def check(self, tool_input: dict[str, object]) -> ModelTicketGuardDecision:
        self.inputs.append(tool_input)
        return ModelTicketGuardDecision(admitted=True, guard_path="fake")


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_ticket(
        self,
        *,
        title: str,
        description: str,
        team: str,
        parent: str | None,
        assignee_id: str | None,
        label_names: Sequence[str] = (),
    ) -> tuple[str, str]:
        self.calls.append(
            {"description": description, "label_names": list(label_names)}
        )
        return "OMN-1", "https://linear.app/x"


def _handler(guard: _Guard, client: _Client) -> HandlerCreateTicket:
    return HandlerCreateTicket(
        linear_client=client, ticket_guard=guard, lane_intake_policy=_POLICY
    )


def _plugin(tmp_path: Path, guard_body: str, policy: dict[str, Any] | None) -> Path:
    """An installed-plugin shape: hooks/lib/<guard>, hooks/config/<policy>."""
    lib = tmp_path / "hooks" / "lib"
    config = tmp_path / "hooks" / "config"
    lib.mkdir(parents=True)
    config.mkdir(parents=True)
    guard = lib / "ticket_creation_guard.py"
    guard.write_text(guard_body, encoding="utf-8")
    guard.chmod(guard.stat().st_mode | stat.S_IXUSR)
    if policy is not None:
        (config / "ticket_creation_policy.json").write_text(
            json.dumps(policy), encoding="utf-8"
        )
    return guard


_ARGV_ECHO = textwrap.dedent(
    """
    import json, os, sys
    open(os.environ["ECHO_FILE"], "w").write(
        json.dumps({"argv": sys.argv[1:], "lane": os.environ.get("ONEX_LANE", "")})
    )
    sys.exit(int(os.environ.get("ECHO_EXIT", "0")))
    """
)


# -- node_create_ticket ------------------------------------------------------


def test_a_create_is_stamped_labelled_and_the_guard_sees_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_REMOTE_LANE", raising=False)
    monkeypatch.setenv("ONEX_LANE", "lane-a")
    guard, client = _Guard(), _Client()
    result = _handler(guard, client).handle(
        ModelCreateTicketRequest(title="t", description="body text")
    )
    assert result.status == "created"
    assert client.calls[0]["description"] == "opened-by: lane-a\n\nbody text"
    assert client.calls[0]["label_names"] == ["the-label"]
    assert guard.inputs[0]["labels"] == ["the-label"]
    assert str(guard.inputs[0]["description"]).split("\n")[0] == "opened-by: lane-a"


def test_the_remote_lane_declaration_is_read_when_the_lane_one_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_LANE", raising=False)
    monkeypatch.setenv("ONEX_REMOTE_LANE", "lane-remote")
    assert declared_lane() == "lane-remote"


def test_a_create_with_no_lane_is_not_stamped_so_the_guard_refuses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No identity to stamp with: the text goes to the guard as written, and the
    guard (run with ``--node``) refuses ``lane_identity_missing``."""
    monkeypatch.delenv("ONEX_LANE", raising=False)
    monkeypatch.delenv("ONEX_REMOTE_LANE", raising=False)
    guard = _Guard()
    _handler(guard, _Client()).handle(
        ModelCreateTicketRequest(title="t", description="plain")
    )
    assert guard.inputs[0]["description"] == "plain"


def test_a_dry_run_asks_the_guard_not_to_spend_the_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_LANE", "lane-a")
    echo = tmp_path / "echo.json"
    guard_path = _plugin(tmp_path / "plugin", _ARGV_ECHO, None)
    monkeypatch.setenv("ECHO_FILE", str(echo))
    handler = HandlerCreateTicket(linear_client=_Client(), lane_intake_policy=_POLICY)
    monkeypatch.setattr(
        "omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket.locate_ticket_guard",
        lambda: guard_path,
    )
    handler.handle(ModelCreateTicketRequest(title="t", description="b", dry_run=True))
    seen = json.loads(echo.read_text(encoding="utf-8"))
    assert seen["argv"] == ["--node", "--no-record"]
    assert seen["lane"] == "lane-a"
    # Control: a real create records, so it passes no --no-record.
    handler.handle(ModelCreateTicketRequest(title="t", description="b"))
    assert json.loads(echo.read_text(encoding="utf-8"))["argv"] == ["--node"]


def test_the_installed_guard_runs_in_node_mode_with_the_lane_in_its_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ECHO_FILE", str(tmp_path / "echo.json"))
    guard = InstalledPluginTicketGuard(
        _plugin(tmp_path / "p", _ARGV_ECHO, None), lane="lane-z"
    )
    assert guard.check({"title": "t"}).admitted is True
    seen = json.loads((tmp_path / "echo.json").read_text(encoding="utf-8"))
    assert seen == {"argv": ["--node"], "lane": "lane-z"}


def test_a_guard_refusal_in_node_mode_is_a_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ECHO_FILE", str(tmp_path / "echo.json"))
    monkeypatch.setenv("ECHO_EXIT", "1")
    guard = InstalledPluginTicketGuard(_plugin(tmp_path / "p", _ARGV_ECHO, None))
    assert guard.check({"title": "t"}).admitted is False


def test_the_gateway_sends_the_label_ids_with_the_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict[str, object]]] = []

    def post(query: str, variables: dict[str, object]) -> dict[str, Any]:
        sent.append((query, variables))
        if "teams(" in query:
            return {"data": {"teams": {"nodes": [{"id": "team-1"}]}}}
        if "workflowStates" in query:
            return {"data": {"workflowStates": {"nodes": [{"id": "state-1"}]}}}
        if "viewer" in query:
            return {"data": {"viewer": {"id": "user-1"}}}
        if "issueLabels" in query:
            return {"data": {"issueLabels": {"nodes": [{"id": "label-1"}]}}}
        return {"data": {"issueCreate": {"issue": {"identifier": "OMN-9", "url": "u"}}}}

    gateway = LinearTicketHttpGateway("k")
    monkeypatch.setattr(gateway, "_post", post)
    gateway.create_ticket(
        title="t",
        description="d",
        team="Omninode",
        parent=None,
        assignee_id="a",
        label_names=["the-label"],
    )
    create = next(v for q, v in sent if "issueCreate" in q)
    assert create["labelIds"] == ["label-1"]
    # Control: no label names, no labelIds key.
    sent.clear()
    gateway.create_ticket(
        title="t", description="d", team="Omninode", parent=None, assignee_id="a"
    )
    assert "labelIds" not in next(v for q, v in sent if "issueCreate" in q)


# -- the shared handler ------------------------------------------------------


def test_stamping_is_idempotent_and_puts_the_stamp_first() -> None:
    once = stamp_description("body", _POLICY, "lane-a")
    assert once == "opened-by: lane-a\n\nbody"
    assert stamp_description(once, _POLICY, "lane-a") == once
    assert stamp_description("body", _POLICY, "lane-b").startswith("opened-by: lane-b")
    assert stamp_description("", _POLICY, "lane-a") == "opened-by: lane-a\n"


def test_an_existing_label_is_reused_and_a_missing_one_is_created() -> None:
    calls: list[str] = []

    def found(query: str, variables: dict[str, object]) -> dict[str, Any]:
        calls.append(query)
        return {"data": {"issueLabels": {"nodes": [{"id": "L1"}]}}}

    assert resolve_label_id(found, "x", "team") == "L1"
    assert len(calls) == 1

    def absent(query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "issueLabelCreate" in query:
            return {"data": {"issueLabelCreate": {"issueLabel": {"id": "L2"}}}}
        return {"data": {"issueLabels": {"nodes": []}}}

    assert resolve_label_id(absent, "x", "team") == "L2"

    def broken(query: str, variables: dict[str, object]) -> dict[str, Any]:
        return {"data": {"issueLabels": {"nodes": []}, "issueLabelCreate": {}}}

    with pytest.raises(RuntimeError):
        resolve_label_id(broken, "x", "team")


def test_the_vocabulary_is_read_from_the_policy_beside_the_installed_guard(
    tmp_path: Path,
) -> None:
    policy = {"lane_intake": {"label": "lbl", "stamp_prefix": "by:"}}
    guard = _plugin(tmp_path, "", policy)
    assert read_lane_intake_policy(guard) == ModelLaneIntakePolicy(
        label="lbl", stamp_prefix="by:"
    )


@pytest.mark.parametrize(
    "policy",
    [None, {}, {"lane_intake": {"label": "lbl"}}],
    ids=["no-file", "no-object", "no-stamp-prefix"],
)
def test_an_unreadable_policy_refuses_rather_than_defaulting(
    tmp_path: Path, policy: dict[str, Any] | None
) -> None:
    with pytest.raises(LaneIntakeRefusedError):
        read_lane_intake_policy(_plugin(tmp_path, "", policy))


@pytest.mark.parametrize(
    ("exit_code", "stdout", "refused"),
    [
        (0, "", False),
        (3, json.dumps({"reason": "budget is spent"}), True),
        (1, "", True),
    ],
    ids=["admitted", "refused-with-reason", "could-not-decide"],
)
def test_admit_maps_the_guard_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exit_code: int,
    stdout: str,
    refused: bool,
) -> None:
    body = f"import sys\nsys.stdout.write({stdout!r})\nsys.exit({exit_code})\n"
    guard = _plugin(tmp_path, body, None)
    if refused:
        with pytest.raises(LaneIntakeRefusedError) as raised:
            admit_lane_intake(guard, {"title": "t"}, "lane-a")
        if exit_code == 3:
            assert "budget is spent" in str(raised.value)
    else:
        admit_lane_intake(guard, {"title": "t"}, "lane-a")


def test_admit_runs_the_guard_in_lane_intake_mode_with_the_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ECHO_FILE", str(tmp_path / "echo.json"))
    admit_lane_intake(
        _plugin(tmp_path / "p", _ARGV_ECHO, None), {"title": "t"}, "lane-q"
    )
    assert json.loads((tmp_path / "echo.json").read_text(encoding="utf-8")) == {
        "argv": ["--lane-intake"],
        "lane": "lane-q",
    }


def test_the_gate_locates_the_guard_lazily_and_reads_its_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    guard = _plugin(tmp_path, "", {"lane_intake": {"label": "l", "stamp_prefix": "s:"}})
    gate = LaneIntakeGate(guard_path=guard)
    assert gate.policy().label == "l"
