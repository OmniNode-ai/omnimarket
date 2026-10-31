# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21676 -- the trusted pre-merge criteria check blocks real drift and anything unreadable.

Runs the real ``HandlerDodCriteriaCheck`` over a recorded GitHub transport and a recorded
ticket tracker, so no request leaves the machine and no credential is read. The contract is the
one the verifier's own drift suite pins to the hash of its criteria text, so the rule under test
is the verifier's, moved to the point that holds the credential.

* A contract with a requirements list and unchanged criteria passes.
* Real criteria drift (an edit, a deletion) blocks the merge at the check.
* An unreadable ticket blocks, whether Linear fails, the ticket is missing, or the tracker
  cannot be built because the key store holds no Linear credential.
* A pull request that moved past the head the caller will merge, one GitHub cannot read and a
  contract that cannot be read all block.
* A contract that records no criteria and a ticket with no contract at the head do not.
"""

from __future__ import annotations

import base64
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.github_landing.github_landing_transport import (
    GithubLandingTransportError,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_dod_criteria_check_effect.handlers.handler_dod_criteria_check import (
    HandlerDodCriteriaCheck,
)
from omnimarket.nodes.node_dod_criteria_check_effect.models import (
    EnumDodCriteriaVerdict,
    ModelDodCriteriaCheckRequest,
    ModelDodCriteriaCheckResult,
)
from tests.test_dod_verify_acceptance_basis import _contract
from tests.unit.nodes.node_dod_verify.omn_20858_ticket_reader import criteria_body
from tests.unit.nodes.node_dod_verify.test_omn_20858_criteria_drift import (
    _pinned_contract,
    _statements,
)

pytestmark = pytest.mark.unit

_HEAD = "c" * 40
_REPO = "OmniNode-ai/omnibase_infra"
_TICKET = "OMN-20999"
_KEY = "-".join(["recorded", "stand", "in", "value"])


class _Github:
    """A recorded pull request and its contracts."""

    def __init__(
        self,
        *,
        title: str = f"fix({_TICKET}): a change",
        head: str = _HEAD,
        state: str = "open",
        contracts: dict[str, str] | None = None,
        pr_status: int = 200,
        contract_status: int | None = None,
        broken: bool = False,
    ) -> None:
        self.title = title
        self.head = head
        self.state = state
        self.contracts = contracts or {}
        self.pr_status = pr_status
        self.contract_status = contract_status
        self.broken = broken
        self.sent: list[str] = []

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request.path)
        if self.broken:
            raise GithubLandingTransportError("no route")
        if "/pulls/" in request.path:
            if self.pr_status != 200:
                return ModelGithubHttpResponse(status=self.pr_status, headers={})
            return ModelGithubHttpResponse(
                status=200,
                headers={},
                body={
                    "title": self.title,
                    "state": self.state,
                    "merged": False,
                    "head": {"sha": self.head},
                },
            )
        path = request.path.split("/contents/", 1)[1].split("?", 1)[0]
        assert request.path.endswith(f"?ref={_HEAD}")
        if self.contract_status is not None:
            return ModelGithubHttpResponse(status=self.contract_status, headers={})
        text = self.contracts.get(path)
        if text is None:
            return ModelGithubHttpResponse(status=404, headers={})
        return ModelGithubHttpResponse(
            status=200,
            headers={},
            body={
                "encoding": "base64",
                "content": base64.b64encode(text.encode()).decode(),
            },
        )


class _Issue:
    def __init__(self, identifier: str, description: str | None) -> None:
        self.identifier = identifier
        self.description = description


class _Tracker:
    """A recorded Linear: serves one body (or fails) and counts reads."""

    def __init__(self, answer: _Issue | BaseException) -> None:
        self.answer = answer
        self.reads = 0
        self.closed = False

    async def get_issue(self, issue_id: str) -> _Issue:
        self.reads += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    async def close(self) -> None:
        self.closed = True


def _contract_text(contract: dict[str, Any]) -> dict[str, str]:
    return {f"contracts/{_TICKET}.yaml": yaml.safe_dump(contract)}


def _tracker_for(body: str) -> _Tracker:
    return _Tracker(_Issue(_TICKET, body))


async def _check(
    github: _Github, tracker: _Tracker | None = None, *, no_credential: bool = False
) -> ModelDodCriteriaCheckResult:
    async def factory() -> _Tracker:
        if no_credential:
            raise RuntimeError(f"api_key_ref 'LINEAR_API_KEY' resolved to None {_KEY}")
        assert tracker is not None
        return tracker

    handler = HandlerDodCriteriaCheck(transport=github, tracker_factory=factory)
    return await handler.handle(
        ModelDodCriteriaCheckRequest(
            correlation_id=uuid4(), repo=_REPO, pr_number=7, head_sha=_HEAD
        )
    )


def _unchanged() -> str:
    return criteria_body(_statements())


@pytest.mark.asyncio
async def test_unchanged_criteria_pass_with_no_hosted_credential() -> None:
    github = _Github(contracts=_contract_text(_pinned_contract()))
    tracker = _tracker_for(_unchanged())
    result = await _check(github, tracker)
    assert result.verdict is EnumDodCriteriaVerdict.PASS
    assert result.merge_blocked is False
    assert result.reason == ""
    assert [t.ticket_id for t in result.tickets] == [_TICKET]
    assert result.tickets[0].criteria_revision
    # Two reads bracket the comparison, as the verifier's do, and the tracker is released.
    assert tracker.reads == 2
    assert tracker.closed


@pytest.mark.asyncio
async def test_an_edited_criterion_blocks_the_merge() -> None:
    edited = {**_statements(), "AC1": "AC1: does a different thing -- falsifier: x"}
    github = _Github(contracts=_contract_text(_pinned_contract()))
    result = await _check(github, _tracker_for(criteria_body(edited)))
    assert result.verdict is EnumDodCriteriaVerdict.DRIFT
    assert result.merge_blocked is True
    assert result.reason.startswith(f"{_TICKET}: CRITERIA_DRIFT")
    assert "AC1 edited" in result.reason
    assert result.tickets[0].amendment is not None


@pytest.mark.asyncio
async def test_a_deleted_criterion_blocks_the_merge() -> None:
    kept = {"AC1": _statements()["AC1"]}
    github = _Github(contracts=_contract_text(_pinned_contract()))
    result = await _check(github, _tracker_for(criteria_body(kept)))
    assert result.verdict is EnumDodCriteriaVerdict.DRIFT
    assert result.merge_blocked is True
    assert "AC2 deleted" in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        KeyError("Issue not found: OMN-20999"),
        ConnectionError(f"unreachable {_KEY}"),
        _Issue("OMN-1", "- AC1: x"),
    ],
)
async def test_an_unreadable_ticket_blocks_the_merge(
    answer: _Issue | BaseException,
) -> None:
    github = _Github(contracts=_contract_text(_pinned_contract()))
    result = await _check(github, _Tracker(answer))
    assert result.verdict is EnumDodCriteriaVerdict.UNREADABLE
    assert result.merge_blocked is True
    assert result.reason.startswith("CRITERIA_UNREADABLE")
    assert _KEY not in result.model_dump_json()


@pytest.mark.asyncio
async def test_a_key_store_with_no_linear_credential_blocks_and_leaks_nothing() -> None:
    github = _Github(contracts=_contract_text(_pinned_contract()))
    result = await _check(github, no_credential=True)
    assert result.verdict is EnumDodCriteriaVerdict.UNREADABLE
    assert result.merge_blocked is True
    assert "LINEAR_API_KEY" in result.reason
    assert _KEY not in result.model_dump_json()


@pytest.mark.asyncio
async def test_a_head_that_moved_past_the_request_blocks() -> None:
    github = _Github(head="d" * 40, contracts=_contract_text(_pinned_contract()))
    result = await _check(github, _tracker_for(_unchanged()))
    assert result.merge_blocked is True
    assert "not the one the caller will merge" in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "github",
    [
        _Github(pr_status=502),
        _Github(state="closed", contracts=_contract_text(_pinned_contract())),
        _Github(broken=True),
        _Github(contract_status=500),
    ],
)
async def test_anything_github_cannot_answer_blocks(github: _Github) -> None:
    result = await _check(github, _tracker_for(_unchanged()))
    assert result.verdict is EnumDodCriteriaVerdict.UNREADABLE
    assert result.merge_blocked is True


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["{not: [valid", "- just\n- a list\n"])
async def test_a_contract_that_cannot_be_parsed_blocks(text: str) -> None:
    github = _Github(contracts={f"contracts/{_TICKET}.yaml": text})
    result = await _check(github, _tracker_for(_unchanged()))
    assert result.verdict is EnumDodCriteriaVerdict.UNREADABLE
    assert result.merge_blocked is True


@pytest.mark.asyncio
async def test_a_contract_recording_no_criteria_is_not_engaged_and_reads_no_ticket() -> (
    None
):
    github = _Github(contracts=_contract_text(_contract(falsifiers={})))
    tracker = _tracker_for(_unchanged())
    result = await _check(github, tracker)
    assert result.verdict is EnumDodCriteriaVerdict.NOT_ENGAGED
    assert result.merge_blocked is False
    assert tracker.reads == 0


@pytest.mark.asyncio
async def test_a_ticket_with_no_contract_at_the_head_is_not_engaged() -> None:
    result = await _check(_Github(), _tracker_for(_unchanged()))
    assert result.verdict is EnumDodCriteriaVerdict.NOT_ENGAGED
    assert result.merge_blocked is False


@pytest.mark.asyncio
async def test_a_title_citing_no_ticket_is_not_engaged() -> None:
    github = _Github(title="chore: a change")
    result = await _check(github, _tracker_for(_unchanged()))
    assert result.verdict is EnumDodCriteriaVerdict.NOT_ENGAGED
    assert github.sent
    assert all("/contents/" not in p for p in github.sent)


@pytest.mark.asyncio
async def test_the_worst_ticket_decides_and_names_only_the_blocking_one() -> None:
    other = "OMN-20998"
    github = _Github(
        title=f"feat({_TICKET}, {other}): two tickets",
        contracts={
            **_contract_text(_pinned_contract()),
            f"contracts/{other}.yaml": yaml.safe_dump(_contract(falsifiers={})),
        },
    )
    edited = {**_statements(), "AC2": "AC2: changed -- falsifier: y"}
    result = await _check(github, _tracker_for(criteria_body(edited)))
    assert result.verdict is EnumDodCriteriaVerdict.DRIFT
    assert {t.ticket_id: t.verdict for t in result.tickets} == {
        _TICKET: EnumDodCriteriaVerdict.DRIFT,
        other: EnumDodCriteriaVerdict.NOT_ENGAGED,
    }
    assert other not in result.reason
