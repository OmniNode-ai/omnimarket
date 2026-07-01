# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-state coverage for node_linear_triage (OMN-13783, WS-M Wave 5).

``EnumTriageAction`` declares 10 action states. Before this suite, the
pre-existing tests (multiparam + OMN-13039/13756/13757/13759 suites) covered
MARK_DONE, WOULD_MARK_DONE, FLAG_STALE, MARK_IN_PROGRESS, WOULD_MARK_IN_PROGRESS
— but MARK_DONE_SUPERSEDED, WOULD_MARK_DONE_SUPERSEDED, MARK_DONE_EPIC, and
WOULD_MARK_DONE_EPIC had zero test references anywhere in the suite. This file
closes that gap and adds negative controls for the OMN-13759 suppression
guards (open children, reopened-after-merge) and the mutation-failure ->
FLAG_STALE exception path.

``EnumTriageAction.NO_CHANGE`` is declared but never assigned by
``HandlerLinearTriage`` — a ticket with no findings simply produces no
``ModelTriageAction`` entry. ``test_no_change_is_a_declared_but_unreachable_state``
documents this explicitly rather than silently leaving it untested: it is a
state-coverage GAP in the contract's declared action set, not a behavior this
suite can honestly assert against real code. Tracked as a state-coverage
finding, not fabricated as passing behavior.

I/O boundary: LinearClientProtocol / GitHubClientProtocol constructor-injected
fakes only. No monkeypatching of urllib/subprocess.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    HandlerLinearTriage,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    EnumTriageAction,
    ModelLinearTriageStartCommand,
)

_NOW = datetime.now(UTC)
_FRESH = _NOW.isoformat()


def _node(
    identifier: str,
    state: str,
    *,
    id: str | None = None,
    updated_at: str = _FRESH,
    parent: str | None = None,
    labels: tuple[str, ...] = ("omnimarket",),
) -> dict[str, Any]:
    return {
        "id": id or f"id-{identifier}",
        "identifier": identifier,
        "title": f"[omnimarket] {identifier} work",
        "state": {"name": state},
        "updatedAt": updated_at,
        "branchName": "",
        "parent": {"id": parent} if parent else None,
        "labels": {"nodes": [{"name": label} for label in labels]},
    }


def _child(identifier: str, state_name: str) -> dict[str, Any]:
    return {"identifier": identifier, "state": {"name": state_name}}


class _FakeLinearClient:
    """Constructor-injected LinearClientProtocol fake.

    ``children`` maps parent ticket id -> list of child issue dicts.
    ``history`` maps ticket id -> list of history event nodes.
    ``raise_on_save`` is a set of issue ids whose ``save_issue`` call raises
    (used for the mutation-failure -> FLAG_STALE negative control).
    """

    def __init__(
        self,
        nodes: list[dict[str, Any]],
        *,
        children: dict[str, list[dict[str, Any]]] | None = None,
        history: dict[str, list[dict[str, Any]]] | None = None,
        raise_on_save: frozenset[str] = frozenset(),
    ) -> None:
        self._nodes = nodes
        self._children = children or {}
        self._history = history or {}
        self._raise_on_save = raise_on_save
        self.saved: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []

    def list_issues(
        self,
        *,
        team: str,
        state_not_in: list[str] | None = None,
        limit: int = 250,
        after: str | None = None,
    ) -> Any:
        return {
            "data": {
                "issues": {
                    "nodes": self._nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }

    def list_children(
        self, *, parent_id: str, limit: int = 50, after: str | None = None
    ) -> Any:
        return {
            "data": {
                "issues": {
                    "nodes": self._children.get(parent_id, []),
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }

    def get_issue(self, *, issue_id: str) -> Any:
        return {"data": {"issue": {}}}

    def list_issue_history(self, *, issue_id: str) -> Any:
        return {
            "data": {"issue": {"history": {"nodes": self._history.get(issue_id, [])}}}
        }

    def save_issue(self, *, issue_id: str, state: str) -> None:
        if issue_id in self._raise_on_save:
            raise RuntimeError("Linear API 500: issueUpdate failed")
        self.saved.append((issue_id, state))

    def save_comment(self, *, issue_id: str, body: str) -> None:
        self.comments.append((issue_id, body))


class _FakeGitHubClient:
    """Constructor-injected GitHubClientProtocol fake.

    ``merged_in_repo`` / ``closed_in_repo`` key on (repo, search_term).
    ``org_wide_merged_sequence`` keys on search_term -> a list of return
    values popped in call order (needed to distinguish the direct-merge
    lookup from the superseded-sibling lookup, which issue the identical
    ``search_prs(search_term=..., state="merged")`` call in sequence).
    """

    def __init__(
        self,
        *,
        merged_in_repo: dict[tuple[str, str], list[dict[str, str]]] | None = None,
        closed_in_repo: dict[tuple[str, str], list[dict[str, str]]] | None = None,
        org_wide_merged_sequence: dict[str, list[list[dict[str, str]]]] | None = None,
    ) -> None:
        self._merged_in_repo = merged_in_repo or {}
        self._closed_in_repo = closed_in_repo or {}
        self._org_wide_sequence = {
            k: list(v) for k, v in (org_wide_merged_sequence or {}).items()
        }

    def search_prs(
        self, *, search_term: str, state: str = "all"
    ) -> list[dict[str, str]]:
        if state != "merged":
            return []
        seq = self._org_wide_sequence.get(search_term)
        if not seq:
            return []
        # Pop in call order; once exhausted, keep returning the last value.
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def search_prs_in_repo(
        self, *, repo: str, search_term: str, state: str = "all"
    ) -> list[dict[str, str]]:
        if state == "merged":
            return self._merged_in_repo.get((repo, search_term), [])
        if state == "closed":
            return self._closed_in_repo.get((repo, search_term), [])
        return []

    def list_prs_by_head(
        self, *, repo: str, branch: str, state: str = "merged"
    ) -> list[dict[str, str]]:
        return []

    def pr_closing_ticket_refs(self, *, repo: str, number: int) -> list[str]:
        return []


def _merged_pr(
    *, number: str, ticket: str, repo: str, merged_at: str
) -> dict[str, str]:
    return {
        "number": number,
        "title": f"fix({ticket}): impl",
        "body": f"Closes {ticket}",
        "state": "closed",
        "mergedAt": merged_at,
        "url": f"https://github.com/OmniNode-ai/{repo}/pull/{number}",
        "repo": repo,
    }


def _closed_unmerged_pr(*, number: str, ticket: str, repo: str) -> dict[str, str]:
    return {
        "number": number,
        "title": f"fix({ticket}): attempt",
        "body": "",
        "state": "closed",
        "mergedAt": "",
        "url": f"https://github.com/OmniNode-ai/{repo}/pull/{number}",
        "repo": repo,
    }


# ---------------------------------------------------------------------------
# MARK_DONE_SUPERSEDED / WOULD_MARK_DONE_SUPERSEDED — zero prior coverage
# ---------------------------------------------------------------------------


def _superseded_setup() -> tuple[_FakeLinearClient, _FakeGitHubClient]:
    ticket = _node("OMN-9600", "In Progress")
    linear = _FakeLinearClient([ticket])
    gh = _FakeGitHubClient(
        # Direct merge lookup (repo-scoped): no match in omnimarket.
        merged_in_repo={("omnimarket", "OMN-9600"): []},
        # Closed-unmerged PR exists in omnimarket (the superseded original).
        closed_in_repo={
            ("omnimarket", "OMN-9600"): [
                _closed_unmerged_pr(number="50", ticket="OMN-9600", repo="omnimarket")
            ]
        },
        # Org-wide "merged" search: [] on the direct-lookup call, then the
        # sibling merged-elsewhere PR on the superseded-check's sibling call.
        org_wide_merged_sequence={
            "OMN-9600": [
                [],
                [
                    _merged_pr(
                        number="55",
                        ticket="OMN-9600",
                        repo="omniclaude",
                        merged_at="2026-06-15T00:00:00Z",
                    )
                ],
            ]
        },
    )
    return linear, gh


@pytest.mark.unit
async def test_would_mark_done_superseded_flag_only() -> None:
    linear, gh = _superseded_setup()
    handler = HandlerLinearTriage(client=linear, github_client=gh)
    # flag_only=True is the default — the outer safety gate.
    result = await handler.handle(ModelLinearTriageStartCommand())

    assert result.marked_done_superseded == 0
    assert linear.saved == []
    would = [
        a
        for a in result.actions
        if a.action == EnumTriageAction.WOULD_MARK_DONE_SUPERSEDED
    ]
    assert would, f"expected WOULD_MARK_DONE_SUPERSEDED; got {result.actions}"
    assert would[0].ticket_id == "OMN-9600"
    assert any("OMN-9600 (superseded)" in entry for entry in result.suppressed_closes)


@pytest.mark.unit
async def test_mark_done_superseded_when_not_flag_only() -> None:
    linear, gh = _superseded_setup()
    handler = HandlerLinearTriage(client=linear, github_client=gh)
    result = await handler.handle(
        ModelLinearTriageStartCommand(flag_only=False, dry_run=False)
    )

    assert result.marked_done_superseded == 1
    assert ("id-OMN-9600", "Done") in linear.saved
    done = [
        a for a in result.actions if a.action == EnumTriageAction.MARK_DONE_SUPERSEDED
    ]
    assert done, f"expected MARK_DONE_SUPERSEDED; got {result.actions}"
    assert done[0].ticket_id == "OMN-9600"


@pytest.mark.unit
async def test_mutation_failure_on_superseded_close_flags_stale() -> None:
    # NEGATIVE CONTROL: save_issue raises during the superseded-close mutation
    # -> the handler must degrade to FLAG_STALE, never crash the sweep.
    linear, gh = _superseded_setup()
    linear = _FakeLinearClient(
        [_node("OMN-9600", "In Progress")],
        raise_on_save=frozenset({"id-OMN-9600"}),
    )
    handler = HandlerLinearTriage(client=linear, github_client=gh)
    result = await handler.handle(
        ModelLinearTriageStartCommand(flag_only=False, dry_run=False)
    )

    assert result.marked_done_superseded == 0
    assert linear.saved == []
    flagged = [
        a
        for a in result.actions
        if a.action == EnumTriageAction.FLAG_STALE and a.ticket_id == "OMN-9600"
    ]
    assert flagged, f"expected mutation-failure FLAG_STALE; got {result.actions}"
    assert "Sibling mutation failed" in flagged[0].evidence


# ---------------------------------------------------------------------------
# MARK_DONE_EPIC / WOULD_MARK_DONE_EPIC — zero prior coverage
# ---------------------------------------------------------------------------


def _epic_all_children_done_setup() -> tuple[_FakeLinearClient, _FakeGitHubClient]:
    epic = _node("OMN-9700", "In Progress", id="epic-id-9700", parent=None)
    linear = _FakeLinearClient(
        [epic],
        children={"epic-id-9700": [_child("OMN-9701", "Done")]},
    )
    gh = _FakeGitHubClient()  # no PRs anywhere -> PR-check phase is a no-op
    return linear, gh


@pytest.mark.unit
async def test_would_mark_done_epic_flag_only() -> None:
    linear, gh = _epic_all_children_done_setup()
    handler = HandlerLinearTriage(client=linear, github_client=gh)
    result = await handler.handle(ModelLinearTriageStartCommand())

    assert result.epics_closed == 0
    assert linear.saved == []
    would = [
        a for a in result.actions if a.action == EnumTriageAction.WOULD_MARK_DONE_EPIC
    ]
    assert would, f"expected WOULD_MARK_DONE_EPIC; got {result.actions}"
    assert would[0].ticket_id == "OMN-9700"
    assert any("OMN-9700 (epic)" in entry for entry in result.suppressed_closes)


@pytest.mark.unit
async def test_mark_done_epic_when_not_flag_only() -> None:
    linear, gh = _epic_all_children_done_setup()
    handler = HandlerLinearTriage(client=linear, github_client=gh)
    result = await handler.handle(
        ModelLinearTriageStartCommand(flag_only=False, dry_run=False)
    )

    assert result.epics_closed == 1
    assert ("epic-id-9700", "Done") in linear.saved
    done = [a for a in result.actions if a.action == EnumTriageAction.MARK_DONE_EPIC]
    assert done, f"expected MARK_DONE_EPIC; got {result.actions}"
    assert done[0].ticket_id == "OMN-9700"


@pytest.mark.unit
async def test_epic_with_open_child_is_not_closed() -> None:
    # NEGATIVE CONTROL: an epic with a non-done child must never close.
    epic = _node("OMN-9702", "In Progress", id="epic-id-9702", parent=None)
    linear = _FakeLinearClient(
        [epic],
        children={"epic-id-9702": [_child("OMN-9703", "In Progress")]},
    )
    gh = _FakeGitHubClient()
    handler = HandlerLinearTriage(client=linear, github_client=gh)
    result = await handler.handle(
        ModelLinearTriageStartCommand(flag_only=False, dry_run=False)
    )

    assert result.epics_closed == 0
    assert linear.saved == []
    epic_actions = [a for a in result.actions if a.ticket_id == "OMN-9702"]
    assert not any(
        a.action
        in (EnumTriageAction.MARK_DONE_EPIC, EnumTriageAction.WOULD_MARK_DONE_EPIC)
        for a in epic_actions
    )


@pytest.mark.unit
async def test_mutation_failure_on_epic_close_flags_stale() -> None:
    # NEGATIVE CONTROL: save_issue raises during the epic-close mutation.
    epic = _node("OMN-9700", "In Progress", id="epic-id-9700", parent=None)
    linear = _FakeLinearClient(
        [epic],
        children={"epic-id-9700": [_child("OMN-9701", "Done")]},
        raise_on_save=frozenset({"epic-id-9700"}),
    )
    gh = _FakeGitHubClient()
    handler = HandlerLinearTriage(client=linear, github_client=gh)
    result = await handler.handle(
        ModelLinearTriageStartCommand(flag_only=False, dry_run=False)
    )

    assert result.epics_closed == 0
    assert linear.saved == []
    flagged = [
        a
        for a in result.actions
        if a.action == EnumTriageAction.FLAG_STALE and a.ticket_id == "OMN-9700"
    ]
    assert flagged, f"expected mutation-failure FLAG_STALE; got {result.actions}"
    assert "Epic mutation failed" in flagged[0].evidence


# ---------------------------------------------------------------------------
# OMN-13759 suppression guards — negative controls for MARK_DONE
# ---------------------------------------------------------------------------


def _direct_merged_setup(
    ticket_id: str, *, ticket_gh_id: str = "id"
) -> _FakeGitHubClient:
    return _FakeGitHubClient(
        merged_in_repo={
            ("omnimarket", ticket_id): [
                _merged_pr(
                    number="42",
                    ticket=ticket_id,
                    repo="omnimarket",
                    merged_at="2026-06-01T00:00:00Z",
                )
            ]
        },
    )


@pytest.mark.unit
async def test_open_children_guard_suppresses_mark_done() -> None:
    # NEGATIVE CONTROL: a parent with a non-done child must not close on its
    # own merged PR — the all-children-done epic path owns that transition.
    parent = _node("OMN-9800", "In Progress", id="parent-id-9800")
    child = _node("OMN-9801", "In Progress", parent="parent-id-9800")
    linear = _FakeLinearClient([parent, child])
    gh = _direct_merged_setup("OMN-9800")
    handler = HandlerLinearTriage(client=linear, github_client=gh)
    result = await handler.handle(
        ModelLinearTriageStartCommand(flag_only=False, dry_run=False)
    )

    assert result.marked_done == 0
    assert linear.saved == []
    parent_actions = [a for a in result.actions if a.ticket_id == "OMN-9800"]
    assert not any(
        a.action in (EnumTriageAction.MARK_DONE, EnumTriageAction.WOULD_MARK_DONE)
        for a in parent_actions
    ), f"open-children guard failed to suppress: {parent_actions}"


@pytest.mark.unit
async def test_reopened_after_merge_guard_suppresses_mark_done() -> None:
    # NEGATIVE CONTROL: ticket reopened Done -> In Progress AFTER the PR merged
    # -> the merge is stale done-evidence, must not auto-close.
    ticket = _node("OMN-9900", "In Progress")
    linear = _FakeLinearClient(
        [ticket],
        history={
            "id-OMN-9900": [
                {
                    "createdAt": "2026-06-05T00:00:00Z",
                    "fromState": {"name": "Done"},
                    "toState": {"name": "In Progress"},
                }
            ]
        },
    )
    gh = _direct_merged_setup("OMN-9900")
    handler = HandlerLinearTriage(client=linear, github_client=gh)
    result = await handler.handle(
        ModelLinearTriageStartCommand(flag_only=False, dry_run=False)
    )

    assert result.marked_done == 0
    assert linear.saved == []
    actions = [a for a in result.actions if a.ticket_id == "OMN-9900"]
    assert not any(
        a.action in (EnumTriageAction.MARK_DONE, EnumTriageAction.WOULD_MARK_DONE)
        for a in actions
    ), f"reopened-after-merge guard failed to suppress: {actions}"


@pytest.mark.unit
async def test_mutation_failure_on_mark_done_flags_stale() -> None:
    # NEGATIVE CONTROL: save_issue raises during the direct-merge close.
    ticket = _node("OMN-9950", "In Progress")
    linear = _FakeLinearClient([ticket], raise_on_save=frozenset({"id-OMN-9950"}))
    gh = _direct_merged_setup("OMN-9950")
    handler = HandlerLinearTriage(client=linear, github_client=gh)
    result = await handler.handle(
        ModelLinearTriageStartCommand(flag_only=False, dry_run=False)
    )

    assert result.marked_done == 0
    assert linear.saved == []
    flagged = [
        a
        for a in result.actions
        if a.action == EnumTriageAction.FLAG_STALE and a.ticket_id == "OMN-9950"
    ]
    assert flagged, f"expected mutation-failure FLAG_STALE; got {result.actions}"
    assert "Mutation failed" in flagged[0].evidence


# ---------------------------------------------------------------------------
# NO_CHANGE — declared but unreachable (state-coverage GAP, documented not faked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_change_is_a_declared_but_unreachable_state() -> None:
    """``EnumTriageAction.NO_CHANGE`` exists on the enum but no code path in
    ``HandlerLinearTriage`` ever assigns it — a ticket with no findings just
    produces zero ``ModelTriageAction`` entries. This test pins that fact so
    a future handler change that starts (or stops) emitting NO_CHANGE is a
    visible, reviewed diff instead of a silent contract-vs-code drift. This is
    a state-coverage FINDING (contract-declared, code-unreachable), not
    exercised production behavior.
    """
    assert EnumTriageAction.NO_CHANGE == "no_change"
    import inspect

    from omnimarket.nodes.node_linear_triage.handlers import handler_linear_triage

    source = inspect.getsource(handler_linear_triage)
    assert "EnumTriageAction.NO_CHANGE" not in source, (
        "EnumTriageAction.NO_CHANGE is now assigned somewhere in the handler — "
        "update this suite with a real coverage test for the new code path "
        "instead of leaving this pin stale."
    )


# ---------------------------------------------------------------------------
# Declared terminal event / publish topic — contract-vs-code coverage pin
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_declares_the_completed_terminal_event() -> None:
    """Pins the node's declared ``terminal_event`` / publish topic literal.

    The runtime auto-wires a ``DispatchResultApplier`` that publishes the
    handler's ``ModelLinearTriageResult`` to this topic for non-projection
    compute nodes (the same pattern documented in
    ``node_dod_verify/test_dispatch_envelope_unwrap.py``) — there is no
    in-handler code path that constructs the event, so the contract
    declaration itself is the thing under test here. A future rename of the
    topic without updating this pin is a visible, reviewed diff.
    """
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_linear_triage"
        / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    expected_topic = "onex.evt.omnimarket.linear-triage-completed.v1"
    assert contract["terminal_event"] == expected_topic
    assert expected_topic in contract["event_bus"]["publish_topics"]
