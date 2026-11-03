# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A dry run makes no Linear write on any node_create_ticket path.

The paths checked, each over a recording fake that logs every request it is handed:

* create, create under a parent, comment, transition, clear_project and repair_project,
  called through ``HandlerCreateTicket``;
* clear_project and repair_project dispatched to ``HandlerTicketProject`` the way
  ``contract.yaml`` routes them, past ``HandlerCreateTicket``;
* the CLI in ``__main__``: create under a parent, comment, transition, clear_project and
  repair_project, each with ``--dry-run``.

A read is not a write; ``read`` takes no dry run and is not in this sweep. repair_project's
dry run lists candidates (a read) and clears none.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from omnimarket.handlers.handler_lane_intake import ModelLaneIntakePolicy
from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
)
from omnimarket.nodes.node_create_ticket import __main__ as cli
from omnimarket.nodes.node_create_ticket.container import container_create_ticket
from omnimarket.nodes.node_create_ticket.handlers import handler_create_ticket
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    ModelTicketGuardDecision,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_ticket_project import (
    HandlerTicketProject,
    LinearTicketProjectGateway,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("lane_create_context")]

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_create_ticket/contract.yaml"
)
_POLICY = ModelLaneIntakePolicy(label="lane-created", stamp_prefix="created-by-lane:")
_TICKET = "OMN-91001"


class _Recorder:
    """Every request any fake below is handed, as ``(seam, what)``."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def log(self, seam: str, what: str) -> None:
        self.requests.append((seam, what))

    def post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.log("graphql", " ".join(query.split()) + f" {variables}")
        return {"data": {}}


class _AdmitGuard:
    def check(self, tool_input: dict[str, object]) -> ModelTicketGuardDecision:
        del tool_input
        return ModelTicketGuardDecision(admitted=True, guard_path="/test/guard.py")


class _RecordingTracker:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    async def get_issue(self, issue_id: str) -> Any:
        self._recorder.log("tracker", f"get_issue {issue_id}")
        return SimpleNamespace()

    async def add_comment(self, issue_id: str, body: str) -> Any:
        self._recorder.log("tracker", f"add_comment {issue_id}")
        return SimpleNamespace(id="comment-1")

    async def update_issue(self, issue_id: str, updates: dict[str, str]) -> Any:
        self._recorder.log("tracker", f"update_issue {issue_id} {updates}")
        return SimpleNamespace(state="Canceled")

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds


class _RecordingStateResolver:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    def resolve_state_id(self, ticket_id: str, state: str) -> str:
        self._recorder.log("state_resolver", f"{ticket_id} {state}")
        return "state-id"


class _RecordingClient:
    """The create seam: ``create_ticket`` is the one Linear write of a create."""

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

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
        self._recorder.log("client", f"create_ticket {title} parent={parent}")
        return "OMN-99999", "https://x/OMN-99999"


def _handler(recorder: _Recorder) -> HandlerCreateTicket:
    return HandlerCreateTicket(
        linear_client=_RecordingClient(recorder),
        tracker=_RecordingTracker(recorder),
        ticket_guard=_AdmitGuard(),
        state_resolver=_RecordingStateResolver(recorder),
        project_gateway=LinearTicketProjectGateway(recorder.post),
        lane_intake_policy=_POLICY,
        principal_resolver=lambda: (_ for _ in ()).throw(
            AssertionError("a dry run resolved the Linear principal")
        ),
    )


def _request(operation: EnumTicketOperation, **kw: object) -> ModelCreateTicketRequest:
    base: dict[str, object] = {"operation": operation, "dry_run": True}
    base.update(kw)
    return ModelCreateTicketRequest.model_validate(base)


_PATHS: dict[str, ModelCreateTicketRequest] = {
    "create": _request(EnumTicketOperation.CREATE, title="t", description="d"),
    "create_under_parent": _request(
        EnumTicketOperation.CREATE, title="t", description="d", parent="OMN-17427"
    ),
    "comment": _request(EnumTicketOperation.COMMENT, ticket_id=_TICKET, body="hello"),
    "transition": _request(
        EnumTicketOperation.TRANSITION, ticket_id=_TICKET, state="Canceled"
    ),
    "clear_project": _request(EnumTicketOperation.CLEAR_PROJECT, ticket_id=_TICKET),
    "repair_project": _request(
        EnumTicketOperation.REPAIR_PROJECT, day="2026-11-02", from_project="Ready"
    ),
}


@pytest.mark.parametrize("path", sorted(_PATHS))
def test_handler_create_ticket_dry_run_writes_nothing(path: str) -> None:
    recorder = _Recorder()
    result = _handler(recorder).handle(_PATHS[path])
    assert result.status == "dry_run"
    assert result.dry_run is True
    assert [r for r in recorder.requests if r[0] != "graphql"] == []
    # No mutation of any kind went through the GraphQL seam. repair_project's dry run
    # lists candidates, which is a read.
    assert not any("mutation" in what.lower() for _, what in recorder.requests)
    assert not any("issueUpdate" in what for _, what in recorder.requests)


@pytest.mark.parametrize(
    ("path", "seam"),
    [
        ("create", "client"),
        ("create_under_parent", "client"),
        ("comment", "tracker"),
        ("transition", "tracker"),
    ],
)
def test_the_recorder_sees_the_write_when_it_is_not_a_dry_run(
    path: str, seam: str
) -> None:
    """Positive control: without dry_run the same request reaches the fake it is recorded on."""
    recorder = _Recorder()
    request = _PATHS[path].model_copy(update={"dry_run": False})
    _handler(recorder).handle(request)
    assert seam in {s for s, _ in recorder.requests}


def test_the_recorder_sees_the_mutation_when_clear_project_is_not_a_dry_run() -> None:
    recorder = _Recorder()
    gateway = LinearTicketProjectGateway(recorder.post)
    with pytest.raises(RuntimeError):
        gateway.clear(_TICKET)
    assert any("issueUpdate" in what for _, what in recorder.requests)


def _routed(
    operation: EnumTicketOperation, recorder: _Recorder
) -> HandlerTicketProject:
    entries = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))["handler_routing"][
        "handlers"
    ]
    [entry] = [e for e in entries if e["operation"] == operation.value]
    module = importlib.import_module(entry["handler"]["module"])
    handler = getattr(module, entry["handler"]["name"])(
        LinearTicketProjectGateway(recorder.post)
    )
    assert isinstance(handler, HandlerTicketProject)
    return handler


@pytest.fixture
def installed_guard_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard = tmp_path / "hooks" / "ticket_creation_guard.py"
    guard.parent.mkdir()
    guard.write_text("", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ticket_creation_policy.json").write_text(
        json.dumps(
            {
                "lane_intake": {
                    "label": _POLICY.label,
                    "stamp_prefix": _POLICY.stamp_prefix,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_TICKET_GUARD_PATH", str(guard))


@pytest.mark.parametrize("path", ["clear_project", "repair_project"])
@pytest.mark.usefixtures("installed_guard_policy")
def test_contract_routed_dry_run_writes_nothing(path: str) -> None:
    recorder = _Recorder()
    request = _PATHS[path]
    result = _routed(request.operation, recorder).handle(request)
    assert result.status == "dry_run"
    assert not any("mutation" in what.lower() for _, what in recorder.requests)
    assert not any("issueUpdate" in what for _, what in recorder.requests)
    if path == "clear_project":
        assert recorder.requests == []


def test_clear_project_dry_run_resolves_no_linear_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default-built handlers (the CLI and the router build them so) reach for no key."""
    reached: list[str] = []

    def _key() -> str:
        reached.append("linear_api_key")
        return "key"

    monkeypatch.setattr(handler_create_ticket, "linear_api_key", _key)
    request = _PATHS["clear_project"]
    assert HandlerTicketProject().handle(request).status == "dry_run"
    assert HandlerCreateTicket().handle(request).status == "dry_run"
    assert reached == []


_CLI_ARGS: dict[str, list[str]] = {
    "create_under_parent": [
        "--title",
        "t",
        "--description",
        "d",
        "--parent",
        "OMN-17427",
    ],
    "comment": ["--operation", "comment", "--ticket-id", _TICKET, "--body", "hello"],
    "transition": [
        "--operation",
        "transition",
        "--ticket-id",
        _TICKET,
        "--state",
        "Canceled",
    ],
    "clear_project": ["--operation", "clear_project", "--ticket-id", _TICKET],
    "repair_project": [
        "--operation",
        "repair_project",
        "--day",
        "2026-11-02",
        "--from-project",
        "Ready",
    ],
}


@pytest.mark.parametrize("path", sorted(_CLI_ARGS))
def test_cli_dry_run_writes_nothing(
    path: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = _Recorder()

    def build() -> HandlerCreateTicket:
        return _handler(recorder)

    monkeypatch.setattr(container_create_ticket, "build_handler_create_ticket", build)
    monkeypatch.setattr(
        "sys.argv", ["node_create_ticket", *_CLI_ARGS[path], "--dry-run"]
    )
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "dry_run"
    assert out["dry_run"] is True
    assert not any("mutation" in what.lower() for _, what in recorder.requests)
    assert not any("issueUpdate" in what for _, what in recorder.requests)
    assert [r for r in recorder.requests if r[0] != "graphql"] == []
