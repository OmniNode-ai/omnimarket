# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A criteria-drift verdict names its writer, measured from the ticket history.

Runs the real ``HandlerDodCriteriaCheck`` over a recorded GitHub transport and a recorded
ticket tracker that also serves the ticket's description-edit history, so no request leaves
the machine and no credential is read.

* No description edit after the contract was written: ``writer=contract`` with the contract
  path and the pull request head, for an added and for a deleted claim.
* An edit after it: ``writer=ticket-edit`` with the actor and the time of the first one.
* A history that cannot be read, a tracker with no history read, or a contract write time
  that cannot be read: ``writer=unmeasured``, never a guess either way.
* The verdict's message starts with the writer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest

from omnimarket.enums.enum_criteria_drift_writer import EnumCriteriaDriftWriter
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.models.model_criteria_drift_writer import (
    ModelCriteriaDriftWriter,
    ModelDescriptionEdit,
    ModelDescriptionEditsRead,
)
from omnimarket.nodes.node_dod_criteria_check_effect.handlers.handler_dod_criteria_check import (
    HandlerDodCriteriaCheck,
)
from omnimarket.nodes.node_dod_criteria_check_effect.models import (
    EnumDodCriteriaVerdict,
    ModelDodCriteriaCheckRequest,
    ModelDodCriteriaCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.criteria_drift_writer import (
    attribute_drift_writer,
)
from omnimarket.nodes.node_dod_verify.services.linear_criteria_tracker import (
    LinearCriteriaTracker,
)
from tests.unit.nodes.node_dod_criteria_check_effect.test_omn_21676_criteria_check import (
    _HEAD,
    _REPO,
    _TICKET,
    _contract_text,
    _Github,
    _Issue,
)
from tests.unit.nodes.node_dod_verify.omn_20858_ticket_reader import criteria_body
from tests.unit.nodes.node_dod_verify.test_omn_20858_criteria_drift import (
    _pinned_contract,
    _statements,
)

pytestmark = pytest.mark.unit

_WRITTEN = datetime(2026, 11, 8, 20, 0, 0, tzinfo=UTC)
_BEFORE = datetime(2026, 11, 8, 4, 7, 0, tzinfo=UTC)
_AFTER = datetime(2026, 11, 8, 22, 30, 15, tzinfo=UTC)
_PATH = f"contracts/{_TICKET}.yaml"
_CONTRACT_WRITER = f"writer=contract {_PATH} at {_HEAD} CRITERIA_DRIFT"


class _GithubWithCommits(_Github):
    """The recorded pull request, plus the commit that last wrote the contract."""

    def __init__(self, *, written: datetime | None = _WRITTEN, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.written = written

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        if "/commits?" in request.path:
            self.sent.append(request.path)
            assert f"sha={_HEAD}" in request.path
            assert f"path=contracts/{_TICKET}.yaml" in request.path
            if self.written is None:
                return ModelGithubHttpResponse(status=500, headers={})
            return ModelGithubHttpResponse(
                status=200,
                headers={},
                body={
                    "value": [
                        {"commit": {"committer": {"date": self.written.isoformat()}}}
                    ]
                },
            )
        return await super().send(request)


class _HistoryTracker:
    """A recorded Linear: the ticket body and the history entries that rewrote it."""

    def __init__(
        self, body: str, history: list[ModelDescriptionEdit] | BaseException
    ) -> None:
        self._body = body
        self._history = history
        self.history_reads = 0

    async def get_issue(self, issue_id: str) -> _Issue:
        return _Issue(_TICKET, self._body)

    async def description_edits(self, issue_id: str) -> list[ModelDescriptionEdit]:
        self.history_reads += 1
        if isinstance(self._history, BaseException):
            raise self._history
        return self._history

    async def close(self) -> None:
        return None


class _HistoryFreeTracker:
    """A tracker that reads tickets and has no history read at all."""

    def __init__(self, body: str) -> None:
        self._body = body

    async def get_issue(self, issue_id: str) -> _Issue:
        return _Issue(_TICKET, self._body)

    async def close(self) -> None:
        return None


def _deleted_ac2() -> str:
    return criteria_body({"AC1": _statements()["AC1"]})


def _added_ac3() -> str:
    return criteria_body(
        {**_statements(), "AC3": "AC3: a slice nobody claimed -- falsifier: z"}
    )


async def _check(
    github: _Github, tracker: _HistoryTracker | _HistoryFreeTracker
) -> ModelDodCriteriaCheckResult:
    async def factory() -> _HistoryTracker | _HistoryFreeTracker:
        return tracker

    handler = HandlerDodCriteriaCheck(transport=github, tracker_factory=factory)
    return await handler.handle(
        ModelDodCriteriaCheckRequest(
            correlation_id=uuid4(), repo=_REPO, pr_number=7, head_sha=_HEAD
        )
    )


def _edit(actor: str, at: datetime) -> ModelDescriptionEdit:
    return ModelDescriptionEdit(actor=actor, created_at=at)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [_deleted_ac2, _added_ac3], ids=["deleted", "added"])
async def test_no_description_edit_after_the_contract_names_the_contract_as_writer(
    body: Any,
) -> None:
    github = _GithubWithCommits(contracts=_contract_text(_pinned_contract()))
    tracker = _HistoryTracker(body(), [_edit("an-earlier-editor", _BEFORE)])
    result = await _check(github, tracker)
    ticket = result.tickets[0]
    assert result.verdict is EnumDodCriteriaVerdict.DRIFT
    assert ticket.detail.startswith(_CONTRACT_WRITER)
    assert result.reason.startswith(f"{_TICKET}: {_CONTRACT_WRITER}")
    assert tracker.history_reads == 1


@pytest.mark.asyncio
async def test_a_ticket_with_no_edit_at_all_names_the_contract_as_writer() -> None:
    github = _GithubWithCommits(contracts=_contract_text(_pinned_contract()))
    result = await _check(github, _HistoryTracker(_deleted_ac2(), []))
    assert result.tickets[0].detail.startswith(_CONTRACT_WRITER)


@pytest.mark.asyncio
async def test_a_description_edit_after_the_contract_names_the_actor_and_time() -> None:
    github = _GithubWithCommits(contracts=_contract_text(_pinned_contract()))
    history = [
        _edit("a-later-editor", datetime(2026, 11, 9, 1, 0, 0, tzinfo=UTC)),
        _edit("the-first-editor", _AFTER),
        _edit("an-earlier-editor", _BEFORE),
    ]
    result = await _check(github, _HistoryTracker(_deleted_ac2(), history))
    detail = result.tickets[0].detail
    assert detail.startswith(
        "writer=ticket-edit the-first-editor 2026-11-08T22:30:15Z "
    )
    assert "writer=contract" not in detail
    assert "AC2 deleted" in detail


@pytest.mark.asyncio
async def test_an_unreadable_history_is_unmeasured_and_never_a_guess() -> None:
    github = _GithubWithCommits(contracts=_contract_text(_pinned_contract()))
    tracker = _HistoryTracker(_deleted_ac2(), ConnectionError("unreachable"))
    result = await _check(github, tracker)
    detail = result.tickets[0].detail
    assert detail.startswith("writer=unmeasured (the ticket history was not read")
    assert "ConnectionError" in detail
    assert "writer=contract" not in detail
    assert "writer=ticket-edit" not in detail
    assert result.merge_blocked is True


@pytest.mark.asyncio
async def test_a_tracker_with_no_history_read_is_unmeasured() -> None:
    github = _GithubWithCommits(contracts=_contract_text(_pinned_contract()))
    result = await _check(github, _HistoryFreeTracker(_deleted_ac2()))
    assert result.tickets[0].detail.startswith(
        "writer=unmeasured (the ticket history was not read: the tracker has no history read)"
    )


@pytest.mark.asyncio
async def test_an_unread_contract_write_time_is_unmeasured() -> None:
    github = _GithubWithCommits(
        written=None, contracts=_contract_text(_pinned_contract())
    )
    tracker = _HistoryTracker(_deleted_ac2(), [_edit("an-editor", _AFTER)])
    result = await _check(github, tracker)
    assert result.tickets[0].detail.startswith(
        f"writer=unmeasured (the write time of {_PATH} at the head was not read)"
    )


@pytest.mark.asyncio
async def test_unchanged_criteria_read_no_history() -> None:
    github = _GithubWithCommits(contracts=_contract_text(_pinned_contract()))
    tracker = _HistoryTracker(
        criteria_body(_statements()), [_edit("an-editor", _AFTER)]
    )
    result = await _check(github, tracker)
    assert result.verdict is EnumDodCriteriaVerdict.PASS
    assert tracker.history_reads == 0


def test_the_pure_attribution_of_each_writer() -> None:
    contract = attribute_drift_writer(
        contract_path=_PATH,
        head_sha=_HEAD,
        written_at=_WRITTEN,
        history=ModelDescriptionEditsRead(ticket_id=_TICKET),
    )
    assert contract.writer is EnumCriteriaDriftWriter.CONTRACT
    edited = attribute_drift_writer(
        contract_path=_PATH,
        head_sha=_HEAD,
        written_at=_WRITTEN,
        history=ModelDescriptionEditsRead(
            ticket_id=_TICKET, edits=(_edit("an-editor", _AFTER),)
        ),
    )
    assert edited.label() == "writer=ticket-edit an-editor 2026-11-08T22:30:15Z"
    unread = attribute_drift_writer(
        contract_path=_PATH,
        head_sha=_HEAD,
        written_at=_WRITTEN,
        history=ModelDescriptionEditsRead(
            ticket_id=_TICKET, unavailable_reason="the history read failed (X)"
        ),
    )
    assert unread.writer is EnumCriteriaDriftWriter.UNMEASURED


def test_a_writer_must_carry_what_its_label_names() -> None:
    with pytest.raises(ValueError, match="names the actor and the time"):
        ModelCriteriaDriftWriter(writer=EnumCriteriaDriftWriter.TICKET_EDIT)
    with pytest.raises(ValueError, match="contract path and the head"):
        ModelCriteriaDriftWriter(writer=EnumCriteriaDriftWriter.CONTRACT)
    with pytest.raises(ValueError, match="names why"):
        ModelCriteriaDriftWriter(writer=EnumCriteriaDriftWriter.UNMEASURED)


class _LinearAdapter:
    """A recorded Linear GraphQL transport serving history pages."""

    def __init__(self, pages: list[httpx.Response]) -> None:
        self.pages = pages
        self.variables: list[dict[str, object] | None] = []

    async def post_graphql(
        self, query: str, variables: dict[str, object] | None = None
    ) -> httpx.Response:
        assert "updatedDescription" in query
        self.variables.append(variables)
        return self.pages[len(self.variables) - 1]

    async def get_issue(self, issue_id: str) -> _Issue:
        return _Issue(issue_id, "")

    async def close(self) -> None:
        return None


def _page(
    nodes: list[dict[str, object]], *, cursor: str | None = None
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "issue": {
                    "history": {
                        "nodes": nodes,
                        "pageInfo": {
                            "hasNextPage": cursor is not None,
                            "endCursor": cursor,
                        },
                    }
                }
            }
        },
    )


@pytest.mark.asyncio
async def test_the_linear_history_read_keeps_description_edits_across_pages() -> None:
    adapter = _LinearAdapter(
        [
            _page(
                [
                    {
                        "createdAt": "2026-11-08T04:07:00.000Z",
                        "updatedDescription": False,
                        "actor": {"name": "a-mover", "displayName": "a-mover"},
                    },
                    {
                        "createdAt": "2026-11-08T22:30:15.000Z",
                        "updatedDescription": True,
                        "actor": {"name": "full-name", "displayName": "short"},
                    },
                ],
                cursor="next",
            ),
            _page(
                [
                    {
                        "createdAt": "2026-11-07T10:00:00.000Z",
                        "updatedDescription": True,
                        "actor": None,
                    }
                ]
            ),
        ]
    )
    edits = await LinearCriteriaTracker(adapter).description_edits(_TICKET)  # type: ignore[arg-type]
    assert [(e.actor, e.created_at.isoformat()) for e in edits] == [
        ("short", "2026-11-08T22:30:15+00:00"),
        ("unnamed-actor", "2026-11-07T10:00:00+00:00"),
    ]
    assert adapter.variables == [{"id": _TICKET}, {"id": _TICKET, "after": "next"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(502),
        httpx.Response(200, json={"errors": [{"message": "boom"}]}),
        httpx.Response(200, json={"data": {"issue": None}}),
    ],
    ids=["http", "errors", "no-issue"],
)
async def test_a_failed_linear_history_read_raises_so_the_writer_is_unmeasured(
    answer: httpx.Response,
) -> None:
    tracker = LinearCriteriaTracker(_LinearAdapter([answer]))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await tracker.description_edits(_TICKET)
