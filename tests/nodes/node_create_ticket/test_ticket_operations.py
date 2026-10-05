# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_create_ticket as the one ticket node: create, read and comment (OMN-20595).

AC1: the node handles create, read and comment, and its contract's
     ``input_model`` names the model ``handle()`` takes.
AC2: a create sends the caller's description to Linear byte for byte.
AC3: a create the installed ticket-creation guard refuses, or for which no
     guard is found, raises and never calls Linear.
"""

from __future__ import annotations

import importlib
import inspect
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    EnumTicketOperation,
    HandlerCreateTicket,
    InstalledPluginTicketGuard,
    ModelCreateTicketRequest,
    ModelTicketGuardDecision,
    TicketGuardRefusedError,
    TicketGuardUnavailableError,
    locate_ticket_guard,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_create_ticket/contract.yaml"
)

_BODY = (
    "Gate: OMN-17427 AC-5\n"
    "\n"
    "Lab lanes need tickets.\n"
    "\n"
    "## Acceptance criteria\n"
    "\n"
    "- [ ] AC1: the node reads a ticket -- falsifier: uv run pytest tests/x.py -k read\n"
)


class _RecordingLinearClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create_ticket(
        self,
        *,
        title: str,
        description: str,
        team: str,
        parent: str | None,
        assignee_id: str | None,
    ) -> tuple[str, str]:
        self.calls.append(
            {
                "title": title,
                "description": description,
                "team": team,
                "parent": parent,
                "assignee_id": assignee_id,
            }
        )
        return "OMN-91001", "https://linear.app/omninode/issue/OMN-91001"


class _FakeGuard:
    def __init__(self, admitted: bool, reason: str = "") -> None:
        self.admitted = admitted
        self.reason = reason
        self.inputs: list[dict[str, object]] = []

    def check(self, tool_input: dict[str, object]) -> ModelTicketGuardDecision:
        self.inputs.append(tool_input)
        return ModelTicketGuardDecision(
            admitted=self.admitted, reason=self.reason, guard_path="/fake/guard.py"
        )


class _FakeTracker:
    def __init__(self) -> None:
        self.closed = False
        self.comments: list[tuple[str, str]] = []

    async def get_issue(self, issue_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            identifier=issue_id,
            title="Read me",
            description="the body",
            state="Backlog",
            url=f"https://linear.app/omninode/issue/{issue_id}",
        )

    async def add_comment(self, issue_id: str, body: str) -> SimpleNamespace:
        self.comments.append((issue_id, body))
        return SimpleNamespace(
            id="comment-1", body=body, author="lab", created_at=datetime.now(UTC)
        )

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
        self.closed = True


# ---------------------------------------------------------------------------
# AC1: operations and the contract's input model
# ---------------------------------------------------------------------------


def test_operations_and_contract_input_model_match_handle() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    dotted = contract["handler"]["input_model"]
    module_name, _, class_name = dotted.rpartition(".")
    declared = getattr(importlib.import_module(module_name), class_name)
    params = list(
        inspect.signature(HandlerCreateTicket.handle, eval_str=True).parameters.values()
    )
    assert params[1].annotation is declared
    assert declared is ModelCreateTicketRequest


def test_operations_and_contract_input_model_declare_every_operation() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    declared = set(contract["inputs"]["operation"]["allowed_values"])
    assert declared == {op.value for op in EnumTicketOperation}
    for field in ModelCreateTicketRequest.model_fields:
        assert field in contract["inputs"], f"contract inputs omit {field!r}"
    for field in contract["inputs"]:
        assert field in ModelCreateTicketRequest.model_fields, (
            f"contract input {field!r} is not a request field"
        )


def test_operations_and_contract_input_model_read_returns_the_ticket() -> None:
    tracker = _FakeTracker()
    handler = HandlerCreateTicket(tracker=tracker)
    result = handler.handle(
        ModelCreateTicketRequest(
            operation=EnumTicketOperation.READ, ticket_id="OMN-20570"
        )
    )
    assert result.status == "read"
    assert result.ticket_id == "OMN-20570"
    assert result.title == "Read me"
    assert result.description_body == "the body"
    assert result.ticket_status == "Backlog"
    assert tracker.closed


def test_operations_and_contract_input_model_comment_posts_the_body() -> None:
    tracker = _FakeTracker()
    handler = HandlerCreateTicket(tracker=tracker)
    result = handler.handle(
        ModelCreateTicketRequest(
            operation=EnumTicketOperation.COMMENT, ticket_id="OMN-20570", body="hello"
        )
    )
    assert result.status == "commented"
    assert result.comment_id == "comment-1"
    assert tracker.comments == [("OMN-20570", "hello")]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operation": "read"},
        {"operation": "comment", "ticket_id": "OMN-1"},
        {"operation": "comment", "body": "x"},
        {"operation": "create"},
        {"operation": "read", "ticket_id": "not a ticket"},
    ],
)
def test_operations_and_contract_input_model_refuse_missing_fields(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="requires"):
        ModelCreateTicketRequest.model_validate(kwargs)


def test_read_without_injected_tracker_fails_loud_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket.resolve_api_key_loop_safe",
        lambda *_a, **_k: None,
    )
    handler = HandlerCreateTicket()
    with pytest.raises(RuntimeError, match="LINEAR_API_KEY"):
        handler.handle(
            ModelCreateTicketRequest(
                operation=EnumTicketOperation.READ, ticket_id="OMN-1"
            )
        )


def test_project_is_refused_naming_the_ruling() -> None:
    with pytest.raises(ValueError, match="Backlog with no project"):
        ModelCreateTicketRequest(title="x", project="Sprint 2026-09-28")


# ---------------------------------------------------------------------------
# AC2: the caller's description passes through unchanged
# ---------------------------------------------------------------------------


def test_description_passes_through_unchanged() -> None:
    client = _RecordingLinearClient()
    guard = _FakeGuard(admitted=True)
    handler = HandlerCreateTicket(linear_client=client, ticket_guard=guard)
    result = handler.handle(
        ModelCreateTicketRequest(
            title="lab lanes read tickets", description=_BODY, parent="OMN-17427"
        )
    )
    assert result.status == "created"
    assert client.calls[0]["description"] == _BODY
    assert result.description_body == _BODY
    assert guard.inputs[0]["description"] == _BODY
    assert "Definition of Done" not in str(client.calls[0]["description"])


def test_description_passes_through_unchanged_to_the_guard_payload() -> None:
    guard = _FakeGuard(admitted=True)
    handler = HandlerCreateTicket(
        linear_client=_RecordingLinearClient(), ticket_guard=guard
    )
    handler.handle(
        ModelCreateTicketRequest(title="t", description=_BODY, parent="OMN-17427")
    )
    assert guard.inputs[0] == {
        "team": "Omninode",
        "title": "t",
        "description": _BODY,
        "parentId": "OMN-17427",
        "state": "Backlog",
    }


# ---------------------------------------------------------------------------
# AC3: the guard's refusal, or no guard, blocks the create
# ---------------------------------------------------------------------------


def test_guard_refusal_blocks_create() -> None:
    client = _RecordingLinearClient()
    handler = HandlerCreateTicket(
        linear_client=client,
        ticket_guard=_FakeGuard(admitted=False, reason="[unfalsified_criterion] x"),
    )
    with pytest.raises(TicketGuardRefusedError, match="unfalsified_criterion"):
        handler.handle(ModelCreateTicketRequest(title="t", description=_BODY))
    assert client.calls == []


def test_guard_refusal_blocks_create_on_dry_run_too() -> None:
    handler = HandlerCreateTicket(ticket_guard=_FakeGuard(admitted=False, reason="no"))
    with pytest.raises(TicketGuardRefusedError):
        handler.handle(ModelCreateTicketRequest(title="t", dry_run=True))


def test_guard_refusal_blocks_create_when_no_guard_is_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ONEX_TICKET_GUARD_PATH", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude"))
    client = _RecordingLinearClient()
    handler = HandlerCreateTicket(linear_client=client)
    with pytest.raises(TicketGuardUnavailableError):
        handler.handle(ModelCreateTicketRequest(title="t", description=_BODY))
    assert client.calls == []


def _write_guard(tmp_path: Path, exit_code: int, stdout: str) -> Path:
    guard = tmp_path / "ticket_creation_guard.py"
    guard.write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "assert payload['tool_name'] == 'mcp__linear-server__save_issue'\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return guard


def test_guard_refusal_blocks_create_through_a_real_subprocess(tmp_path: Path) -> None:
    reason = "BLOCKED:\n  * [unfalsified_criterion] description: AC1 has no falsifier"
    guard = _write_guard(
        tmp_path, 3, json.dumps({"decision": "block", "reason": reason})
    )
    decision = InstalledPluginTicketGuard(guard).check({"title": "t"})
    assert decision.admitted is False
    assert "unfalsified_criterion" in decision.reason
    assert decision.guard_path == str(guard)


def test_guard_admits_through_a_real_subprocess(tmp_path: Path) -> None:
    guard = _write_guard(tmp_path, 0, "")
    assert InstalledPluginTicketGuard(guard).check({"title": "t"}).admitted is True


def test_guard_refusal_blocks_create_when_the_guard_cannot_decide(
    tmp_path: Path,
) -> None:
    guard = _write_guard(tmp_path, 1, "")
    decision = InstalledPluginTicketGuard(guard).check({"title": "t"})
    assert decision.admitted is False
    assert "could not decide" in decision.reason


# ---------------------------------------------------------------------------
# Locating the installed guard
# ---------------------------------------------------------------------------


def test_locate_guard_from_explicit_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    guard = _write_guard(tmp_path, 0, "")
    monkeypatch.setenv("ONEX_TICKET_GUARD_PATH", str(guard))
    assert locate_ticket_guard() == guard


def test_locate_guard_explicit_env_that_does_not_exist_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_TICKET_GUARD_PATH", str(tmp_path / "absent.py"))
    with pytest.raises(TicketGuardUnavailableError, match="ONEX_TICKET_GUARD_PATH"):
        locate_ticket_guard()


def _claude_dir(tmp_path: Path, enabled: dict[str, bool]) -> Path:
    claude = tmp_path / ".claude"
    plugins: dict[str, list[dict[str, str]]] = {}
    for key in enabled:
        install = tmp_path / "cache" / key.replace("@", "_")
        lib = install / "hooks" / "lib"
        lib.mkdir(parents=True)
        (lib / "ticket_creation_guard.py").write_text("", encoding="utf-8")
        plugins[key] = [{"installPath": str(install)}]
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": plugins}), encoding="utf-8"
    )
    (claude / "settings.json").write_text(
        json.dumps({"enabledPlugins": enabled}), encoding="utf-8"
    )
    return claude


def test_locate_guard_from_the_enabled_installed_onex_plugin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    claude = _claude_dir(
        tmp_path, {"onex@omninode-tools": False, "onex@omninode-tools-dev": True}
    )
    monkeypatch.delenv("ONEX_TICKET_GUARD_PATH", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    found = locate_ticket_guard()
    assert "onex_omninode-tools-dev" in str(found)


def test_locate_guard_refuses_two_enabled_onex_plugins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    claude = _claude_dir(tmp_path, {"onex@a": True, "onex@b": True})
    monkeypatch.delenv("ONEX_TICKET_GUARD_PATH", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    with pytest.raises(TicketGuardUnavailableError, match="more than one"):
        locate_ticket_guard()


def test_guard_runs_on_this_interpreter() -> None:
    # The guard is standard library only; the node runs it on its own interpreter.
    assert InstalledPluginTicketGuard(Path("/x")).interpreter == sys.executable
