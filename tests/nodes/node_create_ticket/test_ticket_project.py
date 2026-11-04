# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_create_ticket clears a project Linear copied from a parent, and repairs the ones it missed.

The defect: Linear copies a parent's project onto a sub-issue created without one, so every
lane-created child of a parent in project Ready came out in Ready, against the ruling that new
tickets are Backlog with no project. The documented recovery, an update with ``project=null``,
has no spelling in the Linear MCP (the string arrives as the text ``null``), and the create gate
sees only the call. The node performs the one spelling that works, ``issueUpdate(projectId: null)``,
and reads the issue back independently before it reports success.

The Linear responses come from ``tests/fixtures/node_create_ticket_project/linear_responses.json``,
recorded from the live workspace. ``_FakeLinear`` serves them through the same ``post(query,
variables)`` seam the real gateway uses, so the GraphQL text the node sends is what is checked.
"""

from __future__ import annotations

import copy
import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.handler_lane_intake import ModelLaneIntakePolicy
from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    LinearTicketHttpGateway,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_ticket_project import (
    HandlerTicketProject,
    LinearTicketProjectGateway,
    TicketProjectNotClearedError,
)

pytestmark = pytest.mark.unit

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures/node_create_ticket_project/linear_responses.json"
)
_RECORDED: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_POLICY = ModelLaneIntakePolicy(label="lane-created", stamp_prefix="created-by-lane:")
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_create_ticket/contract.yaml"
)
_IN_READY = "OMN-22207"
_NO_PROJECT = "OMN-21723"


class _FakeLinear:
    """Serves the recorded responses through the gateway's ``post`` seam, with state.

    ``issues`` maps an identifier to its recorded read response; ``stuck`` makes the mutation
    report success while leaving the project in place, the way an update Linear ignores behaves.
    """

    def __init__(self, *, stuck: bool = False) -> None:
        self.stuck = stuck
        self.calls: list[tuple[str, dict[str, Any]]] = []
        ready = _RECORDED["read_in_ready"]
        self.issues: dict[str, dict[str, Any]] = {
            _IN_READY: copy.deepcopy(ready),
            "OMN-22174": self._clone(ready, "OMN-22174", "2026-11-02T20:50:01.850Z"),
            "OMN-22163": self._clone(ready, "OMN-22163", "2026-11-02T17:59:59.077Z"),
            "OMN-22156": self._clone(ready, "OMN-22156", "2026-11-02T17:35:36.916Z"),
            _NO_PROJECT: copy.deepcopy(_RECORDED["read_no_project"]),
        }
        self.listing: dict[str, Any] = copy.deepcopy(_RECORDED["list_ready_2026_11_02"])

    @staticmethod
    def _clone(read: dict[str, Any], ident: str, created: str) -> dict[str, Any]:
        out = copy.deepcopy(read)
        out["data"]["issue"]["identifier"] = ident
        out["data"]["issue"]["createdAt"] = created
        return out

    def mutations(self) -> list[tuple[str, dict[str, Any]]]:
        return [c for c in self.calls if "issueUpdate" in c[0]]

    def post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, variables))
        if "issueUpdate" in query:
            ident = variables["id"]
            if not self.stuck:
                self.issues[ident]["data"]["issue"]["project"] = None
            return {
                "data": {
                    "issueUpdate": {
                        "success": True,
                        "issue": {
                            "identifier": ident,
                            "project": self.issues[ident]["data"]["issue"]["project"],
                        },
                    }
                }
            }
        if "issues(" in query:
            return copy.deepcopy(self.listing)
        return copy.deepcopy(self.issues[variables["id"]])


def _gateway_over(
    post: Callable[[str, dict[str, Any]], Any],
) -> LinearTicketHttpGateway:
    """The real HTTP gateway with its one network method replaced by ``post``."""

    class _Offline(LinearTicketHttpGateway):
        def _post(self, query: str, variables: dict[str, Any]) -> Any:
            return post(query, variables)

    return _Offline("test-key-not-used")


def _handler(fake: _FakeLinear) -> HandlerCreateTicket:
    return HandlerCreateTicket(
        project_gateway=LinearTicketProjectGateway(fake.post),
        lane_intake_policy=_POLICY,
    )


def _clear(ticket_id: str) -> ModelCreateTicketRequest:
    return ModelCreateTicketRequest(
        operation=EnumTicketOperation.CLEAR_PROJECT, ticket_id=ticket_id
    )


def _repair(day: str, *, dry_run: bool = False) -> ModelCreateTicketRequest:
    return ModelCreateTicketRequest(
        operation=EnumTicketOperation.REPAIR_PROJECT,
        day=day,
        from_project="Ready",
        dry_run=dry_run,
    )


# --- AC1: the clear is a direct issueUpdate(projectId: null) and is read back -----------------


def test_clear_sends_issue_update_with_an_explicit_null_project() -> None:
    fake = _FakeLinear()
    result = _handler(fake).handle(_clear(_IN_READY))
    assert result.status == "project_cleared"
    assert result.project_cleared is True
    assert result.ticket_id == _IN_READY
    [(query, variables)] = fake.mutations()
    assert "projectId: null" in query
    assert variables == {"id": _IN_READY}


def test_clear_reads_the_issue_back_after_the_update_not_the_update_response() -> None:
    fake = _FakeLinear()
    _handler(fake).handle(_clear(_IN_READY))
    kinds = ["write" if "issueUpdate" in q else "read" for q, _ in fake.calls]
    assert kinds[-2:] == ["write", "read"]
    assert fake.issues[_IN_READY]["data"]["issue"]["project"] is None


def test_clear_reports_failure_when_linear_ignores_the_update() -> None:
    """RED control: an update that succeeds and leaves the project in place is not success."""
    fake = _FakeLinear(stuck=True)
    with pytest.raises(TicketProjectNotClearedError, match="Ready"):
        _handler(fake).handle(_clear(_IN_READY))


def test_clear_requires_an_explicit_null_in_the_readback() -> None:
    """A readback that omits the project key is not an explicit null, so it is not success."""
    fake = _FakeLinear()
    original = fake.post

    def no_key(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        out = original(query, variables)
        if "issueUpdate" not in query and "issues(" not in query:
            out["data"]["issue"].pop("project", None)
        return out

    handler = HandlerCreateTicket(
        project_gateway=LinearTicketProjectGateway(no_key), lane_intake_policy=_POLICY
    )
    with pytest.raises(TicketProjectNotClearedError, match="explicit null"):
        handler.handle(_clear(_IN_READY))


def test_recorded_real_clear_response_has_the_shape_the_gateway_reads() -> None:
    update = _RECORDED["clear_on_ticket_with_no_project"]["data"]["issueUpdate"]
    assert update["success"] is True
    assert update["issue"]["project"] is None
    assert _RECORDED["read_after_clear"]["data"]["issue"]["project"] is None


def test_the_mcp_spelling_never_reaches_the_node() -> None:
    """The text the MCP sends for null names a project that does not exist (recorded)."""
    assert _RECORDED["mcp_null_text_error"] == 'Could not find project "null"'
    with pytest.raises(ValueError, match="ticket_id"):
        ModelCreateTicketRequest(operation=EnumTicketOperation.CLEAR_PROJECT)


# --- AC2: the repair lists lane-created tickets in Ready for one UTC day and rechecks ----------


def test_repair_clears_each_listed_ticket_and_names_them() -> None:
    fake = _FakeLinear()
    result = _handler(fake).handle(_repair("2026-11-02"))
    assert result.status == "project_repaired"
    assert result.cleared_ids == ["OMN-22207", "OMN-22174", "OMN-22163", "OMN-22156"]
    assert len(fake.mutations()) == 4
    assert all(i["data"]["issue"]["project"] is None for k, i in fake.issues.items())


def test_repair_lists_with_the_explicit_utc_day_label_and_project() -> None:
    fake = _FakeLinear()
    _handler(fake).handle(_repair("2026-11-02", dry_run=True))
    [(_query, variables)] = [c for c in fake.calls if "issues(" in c[0]]
    assert variables["from"] == "2026-11-02T00:00:00.000Z"
    assert variables["to"] == "2026-11-03T00:00:00.000Z"
    assert variables["label"] == "lane-created"
    assert variables["project"] == "Ready"


def test_repair_dry_run_lists_and_writes_nothing() -> None:
    fake = _FakeLinear()
    result = _handler(fake).handle(_repair("2026-11-02", dry_run=True))
    assert result.status == "dry_run"
    assert result.candidate_ids == ["OMN-22207", "OMN-22174", "OMN-22163", "OMN-22156"]
    assert fake.mutations() == []


def test_repair_skips_a_ticket_whose_recheck_no_longer_matches() -> None:
    fake = _FakeLinear()
    fake.issues["OMN-22174"]["data"]["issue"]["createdAt"] = "2026-11-01T23:59:59.000Z"
    fake.issues["OMN-22163"]["data"]["issue"]["labels"]["nodes"] = []
    fake.issues["OMN-22156"]["data"]["issue"]["project"] = {"name": "Sprint"}
    result = _handler(fake).handle(_repair("2026-11-02"))
    assert result.cleared_ids == ["OMN-22207"]
    assert [s.split(":")[0] for s in result.skipped] == [
        "OMN-22174",
        "OMN-22163",
        "OMN-22156",
    ]
    assert len(fake.mutations()) == 1
    assert fake.issues["OMN-22156"]["data"]["issue"]["project"] == {"name": "Sprint"}


def test_repair_for_a_day_with_no_match_is_a_clean_empty_run() -> None:
    fake = _FakeLinear()
    fake.listing = copy.deepcopy(_RECORDED["list_ready_2026_11_03"])
    result = _handler(fake).handle(_repair("2026-11-03"))
    assert result.status == "project_repaired"
    assert result.cleared_ids == []
    assert fake.mutations() == []


def test_repair_follows_every_page_of_the_listing() -> None:
    fake = _FakeLinear()
    pages = [
        {
            "data": {
                "issues": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                    "nodes": [{"identifier": "OMN-22207"}],
                }
            }
        },
        {
            "data": {
                "issues": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"identifier": "OMN-22174"}],
                }
            }
        },
    ]
    original = fake.post

    def paged(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "issues(" in query:
            fake.calls.append((query, variables))
            return pages[0] if variables.get("after") is None else pages[1]
        return original(query, variables)

    handler = HandlerCreateTicket(
        project_gateway=LinearTicketProjectGateway(paged), lane_intake_policy=_POLICY
    )
    result = handler.handle(_repair("2026-11-02", dry_run=True))
    assert result.candidate_ids == ["OMN-22207", "OMN-22174"]


def test_repair_that_cannot_clear_a_ticket_fails_and_names_it() -> None:
    fake = _FakeLinear(stuck=True)
    result = _handler(fake).handle(_repair("2026-11-02"))
    assert result.status == "error"
    assert result.cleared_ids == []
    assert any("OMN-22207" in e for e in result.validation_errors)


@pytest.mark.parametrize(
    "day", ["", "2026-11-3", "2026-13-01", "yesterday", "2026-11-02T00:00Z"]
)
def test_repair_requires_one_explicit_utc_day(day: str) -> None:
    with pytest.raises(ValueError, match="day"):
        ModelCreateTicketRequest(
            operation=EnumTicketOperation.REPAIR_PROJECT, day=day, from_project="Ready"
        )


def test_repair_requires_the_project_to_clear() -> None:
    with pytest.raises(ValueError, match="from_project"):
        ModelCreateTicketRequest(
            operation=EnumTicketOperation.REPAIR_PROJECT, day="2026-11-02"
        )


# --- the create path: a child under a parent that returns a project is cleared at creation ----


def test_create_under_a_parent_clears_an_inherited_project_and_reads_it_back() -> None:
    fake = _FakeLinear()
    fake.issues["OMN-99001"] = _FakeLinear._clone(
        _RECORDED["read_in_ready"], "OMN-99001", "2026-11-03T03:00:00.000Z"
    )
    from omnimarket.nodes.node_create_ticket.handlers.handler_ticket_project import (
        clear_inherited_project,
    )

    clear_inherited_project(LinearTicketProjectGateway(fake.post), "OMN-99001")
    assert fake.issues["OMN-99001"]["data"]["issue"]["project"] is None
    assert len(fake.mutations()) == 1


def test_a_child_that_inherited_no_project_sends_no_update() -> None:
    fake = _FakeLinear()
    from omnimarket.nodes.node_create_ticket.handlers.handler_ticket_project import (
        clear_inherited_project,
    )

    clear_inherited_project(LinearTicketProjectGateway(fake.post), _NO_PROJECT)
    assert fake.mutations() == []


def test_handler_exposes_the_typed_project_handler() -> None:
    handler = HandlerTicketProject(LinearTicketProjectGateway(_FakeLinear().post))
    result = handler.handle(_clear(_IN_READY))
    assert result.project_cleared is True


def test_http_gateway_create_under_a_parent_clears_the_inherited_project() -> None:
    """The real create path: issueCreate under a parent, then the clear and the readback."""
    fake = _FakeLinear()
    fake.issues["OMN-99002"] = _FakeLinear._clone(
        _RECORDED["read_in_ready"], "OMN-99002", "2026-11-03T03:00:00.000Z"
    )

    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "GetTeamByName" in query:
            return {"data": {"teams": {"nodes": [{"id": "team-1"}]}}}
        if "GetBacklogState" in query:
            return {"data": {"workflowStates": {"nodes": [{"id": "state-1"}]}}}
        if "GetIssueByIdentifier" in query:
            return {"data": {"issue": {"id": "parent-uuid"}}}
        if "CreateIssue" in query:
            return {
                "data": {
                    "issueCreate": {
                        "issue": {
                            "identifier": "OMN-99002",
                            "url": "https://x/OMN-99002",
                        }
                    }
                }
            }
        return fake.post(query, variables)

    gateway = _gateway_over(post)
    ident, _ = gateway.create_ticket(
        title="t",
        description="d",
        team="Omninode",
        parent="OMN-17427",
        assignee_id="user-1",
    )
    assert ident == "OMN-99002"
    assert len(fake.mutations()) == 1
    assert fake.issues["OMN-99002"]["data"]["issue"]["project"] is None


def test_http_gateway_create_without_a_parent_reads_back_and_sends_no_clear() -> None:
    fake = _FakeLinear()
    fake.issues["OMN-99003"] = copy.deepcopy(_RECORDED["read_no_project"])

    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "GetTeamByName" in query:
            return {"data": {"teams": {"nodes": [{"id": "team-1"}]}}}
        if "GetBacklogState" in query:
            return {"data": {"workflowStates": {"nodes": [{"id": "state-1"}]}}}
        if "CreateIssue" in query:
            return {
                "data": {
                    "issueCreate": {
                        "issue": {
                            "identifier": "OMN-99003",
                            "url": "https://x/OMN-99003",
                        }
                    }
                }
            }
        return fake.post(query, variables)

    gateway = _gateway_over(post)
    gateway.create_ticket(
        title="t", description="d", team="Omninode", parent=None, assignee_id="user-1"
    )
    assert len(fake.calls) == 1  # the independent readback
    assert fake.mutations() == []


# --- the contract-routed path: the request reaches HandlerTicketProject without HandlerCreateTicket ----


def _routed_handler(
    operation: EnumTicketOperation, gateway: LinearTicketProjectGateway
) -> HandlerTicketProject:
    """The handler contract.yaml names for ``operation``, built the way a router builds it."""
    entries = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))["handler_routing"][
        "handlers"
    ]
    [entry] = [e for e in entries if e["operation"] == operation.value]
    module = importlib.import_module(entry["handler"]["module"])
    handler = getattr(module, entry["handler"]["name"])(gateway)
    assert isinstance(handler, HandlerTicketProject)
    return handler


@pytest.fixture
def installed_guard_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ModelLaneIntakePolicy:
    """An installed ticket guard with its policy file beside it, as production locates it."""
    guard = tmp_path / "hooks" / "ticket_creation_guard.py"
    guard.parent.mkdir()
    guard.write_text("", encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    (config / "ticket_creation_policy.json").write_text(
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
    return _POLICY


def test_contract_routed_clear_project_dry_run_sends_nothing() -> None:
    """AC1: a dry run reaches no Linear write when the contract routes it past HandlerCreateTicket."""
    fake = _FakeLinear()
    handler = _routed_handler(
        EnumTicketOperation.CLEAR_PROJECT, LinearTicketProjectGateway(fake.post)
    )
    result = handler.handle(
        ModelCreateTicketRequest(
            operation=EnumTicketOperation.CLEAR_PROJECT,
            ticket_id=_IN_READY,
            dry_run=True,
        )
    )
    mutating = [q for q, _ in fake.calls if q.lstrip().startswith("mutation")]
    assert mutating == []
    assert fake.mutations() == []
    assert result.status == "dry_run"
    assert result.dry_run is True
    assert result.project_cleared is False
    assert result.ticket_id == _IN_READY
    assert fake.issues[_IN_READY]["data"]["issue"]["project"] is not None


def test_contract_routed_clear_project_still_clears_when_not_a_dry_run() -> None:
    """Positive control: the same routed handler does send the update without dry_run."""
    fake = _FakeLinear()
    handler = _routed_handler(
        EnumTicketOperation.CLEAR_PROJECT, LinearTicketProjectGateway(fake.post)
    )
    result = handler.handle(_clear(_IN_READY))
    assert result.status == "project_cleared"
    assert len(fake.mutations()) == 1


def test_contract_routed_repair_project_routed_uses_the_lane_intake_label(
    installed_guard_policy: ModelLaneIntakePolicy,
) -> None:
    """AC2: the routed repair_project takes its label from the policy, not an empty label."""
    fake = _FakeLinear()
    handler = _routed_handler(
        EnumTicketOperation.REPAIR_PROJECT, LinearTicketProjectGateway(fake.post)
    )
    result = handler.handle(_repair("2026-11-02", dry_run=True))
    assert result.status == "dry_run"
    assert result.candidate_ids == ["OMN-22207", "OMN-22174", "OMN-22163", "OMN-22156"]
    [(_query, variables)] = [c for c in fake.calls if "issues(" in c[0]]
    assert variables["label"] == installed_guard_policy.label
    assert fake.mutations() == []


def test_contract_routed_repair_project_routed_clears_with_the_policy_label(
    installed_guard_policy: ModelLaneIntakePolicy,
) -> None:
    fake = _FakeLinear()
    handler = _routed_handler(
        EnumTicketOperation.REPAIR_PROJECT, LinearTicketProjectGateway(fake.post)
    )
    result = handler.handle(_repair("2026-11-02"))
    assert result.status == "project_repaired"
    assert len(result.cleared_ids) == 4
    assert installed_guard_policy.label == "lane-created"
