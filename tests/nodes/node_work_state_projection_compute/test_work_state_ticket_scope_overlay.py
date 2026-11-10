# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor reads the shared ticket_scope overlay's projects, and the chain walk reads ancestors outside the open list (OMN-23184).

The Factory project reached the goal_scope overlay never, so `scope.in_scope_projects` stayed empty and the Factory tickets
fell out of the work list. The ticket_scope overlay names the projects (`project_ids`, `project_names`); an absent overlay
changes nothing. The Linear snapshot holds the operator's open assigned issues only, so a chain through an unassigned or
closed ancestor reads the snapshot's `ancestors` rows, which are never work items.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.work_state_projection import (
    EnumScopeStatus,
    EnumWorkStateSource,
    ModelWorkScope,
    ModelWorkStateRequest,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.work_state_source_mapping import (
    ancestors_from_linear_snapshot,
    linear_rows_from_snapshot,
    scope_from_declaration,
    tickets_from_linear_snapshot,
)
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import (
    build_request,
    source_readers,
)
from tests.nodes.node_linear_snapshot_effect.test_ancestors import chain, doc
from tests.nodes.node_linear_snapshot_effect.test_rate_limit_backoff import (
    T0,
    Clock,
    Script,
    node,
    page,
    run,
)

pytestmark = pytest.mark.unit

FACTORY_ID = "ae87f221-ad60-49fe-aeae-b4417960ff90"
OVERLAY_ENV = "ONEX_TICKET_SCOPE_OVERLAY"
ROOT = "OMN-22478"

TICKET_SCOPE = f"""\
endpoint: https://example.invalid/linear
cache_seconds: 300
project_ids:
  - {FACTORY_ID}
project_names:
  - Factory
"""


def _issue(ident: str, parent_id: str | None = None, **extra: object) -> dict:
    return {
        "id": ident,
        "title": f"title of {ident}",
        "status": "In Progress",
        "statusType": "started",
        "assignee": "user-1",
        "project": "",
        "updatedAt": "2026-11-08T00:00:00Z",
        "description": "",
        "parentId": parent_id,
        **extra,
    }


def _request(rules, now, home: Path, env: dict[str, str], snapshot: dict):
    def read_linear():
        from omnimarket.nodes.node_work_state_projection_compute.handlers.work_state_source_budget import (
            SourceRead,
        )

        return SourceRead(items=tuple(linear_rows_from_snapshot(snapshot, rules)))

    return build_request(
        now,
        home,
        env,
        None,
        rules,
        reader_overrides={EnumWorkStateSource.LINEAR: read_linear},
    )


def _scope_of(request: ModelWorkStateRequest, key: str) -> EnumScopeStatus:
    result = HandlerWorkStateProjection().handle(request)
    (found,) = [i for i in result.items if i.key == key]
    return found.scope


def _overlay(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "ticket_scope.yaml"
    path.write_text(TICKET_SCOPE, encoding="utf-8")
    return {OVERLAY_ENV: str(path)}


def test_the_ticket_scope_overlay_projects_reach_in_scope_projects() -> None:
    scope = scope_from_declaration(
        {"in_scope_projects": ["Delegation"]},
        None,
        {"project_ids": [FACTORY_ID], "project_names": ["Factory"], "cache_seconds": 5},
    )
    assert scope.in_scope_projects == ("Delegation", FACTORY_ID, "Factory")


def test_an_absent_ticket_scope_overlay_changes_nothing() -> None:
    declaration = {"in_scope_projects": ["Delegation"], "in_scope_tickets": [ROOT]}
    assert scope_from_declaration(declaration, None, None) == scope_from_declaration(
        declaration, None
    )
    assert scope_from_declaration(declaration, None, {}).in_scope_projects == (
        "Delegation",
    )


def test_the_contract_names_the_ticket_scope_overlay() -> None:
    from omnimarket.nodes.node_work_state_projection_effect import (
        runtime_work_state,
    )

    cfg = runtime_work_state._runtime()["ticket_scope_overlay"]
    assert cfg == {"overlay_id": "ticket_scope", "file_env": OVERLAY_ENV}


@pytest.mark.parametrize(
    "issue",
    [
        _issue("OMN-500", projectId=FACTORY_ID),
        _issue("OMN-500", project="Factory"),
    ],
    ids=["by-project-id", "by-project-name"],
)
def test_a_ticket_in_a_ticket_scope_project_is_in_scope(
    rules, now, tmp_path, issue
) -> None:
    snapshot = {"operator": "user-1", "issues": [issue]}
    with_overlay = _request(rules, now, tmp_path, _overlay(tmp_path), snapshot)
    assert with_overlay.scope.in_scope_projects == (FACTORY_ID, "Factory")
    assert _scope_of(with_overlay, "ticket:OMN-500") is EnumScopeStatus.IN_SCOPE
    # Failing control: with no overlay the same ticket is out of scope.
    without = _request(rules, now, tmp_path, {}, snapshot)
    assert without.scope.in_scope_projects == ()
    assert _scope_of(without, "ticket:OMN-500") is EnumScopeStatus.PAUSED


def test_the_ticket_scope_overlay_is_found_by_its_id_under_the_overlay_roots(
    rules, now, tmp_path
) -> None:
    overlay_dir = tmp_path / "roots" / "ticket_scope"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "overlay.yaml").write_text(TICKET_SCOPE, encoding="utf-8")
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "roots")}
    snapshot = {"operator": "user-1", "issues": [_issue("OMN-501", project="Factory")]}
    request = _request(rules, now, tmp_path, env, snapshot)
    assert _scope_of(request, "ticket:OMN-501") is EnumScopeStatus.IN_SCOPE


def test_the_snapshot_ancestors_become_lookup_rows_not_tickets(rules) -> None:
    snapshot = {
        "operator": "user-1",
        "issues": [_issue("OMN-23229", "OMN-23127")],
        "ancestors": [
            {
                "id": "OMN-23127",
                "parentId": "OMN-22880",
                "project": "",
                "projectId": "",
            },
            {
                "id": "OMN-22880",
                "parentId": ROOT,
                "project": "Factory",
                "projectId": FACTORY_ID,
            },
        ],
    }
    assert [t.ticket_id for t in tickets_from_linear_snapshot(snapshot, rules)] == [
        "OMN-23229"
    ]
    rows = ancestors_from_linear_snapshot(snapshot)
    assert [(r.ticket_id, r.parent) for r in rows] == [
        ("OMN-23127", "OMN-22880"),
        ("OMN-22880", ROOT),
    ]
    assert rows[1].project == "Factory"
    assert rows[1].project_id == FACTORY_ID
    # An old file with no key has none.
    assert ancestors_from_linear_snapshot({"operator": "u", "issues": []}) == []


def _chain_snapshot() -> dict:
    return {
        "operator": "user-1",
        "issues": [_issue("OMN-23229", "OMN-23127")],
        "ancestors": [
            {
                "id": "OMN-23127",
                "parentId": "OMN-22880",
                "project": "",
                "projectId": "",
            },
            {"id": "OMN-22880", "parentId": ROOT, "project": "", "projectId": ""},
        ],
    }


def test_a_chain_through_ancestors_outside_the_issue_list_is_in_scope(
    rules, now, tmp_path
) -> None:
    snapshot = _chain_snapshot()
    scope = ModelWorkScope(in_scope_tickets=(ROOT,))
    with_ancestors = _request(rules, now, tmp_path, {}, snapshot).model_copy(
        update={"scope": scope}
    )
    assert _scope_of(with_ancestors, "ticket:OMN-23229") is EnumScopeStatus.IN_SCOPE
    # The ancestors are lookup rows only: no work item for them.
    keys = {i.key for i in HandlerWorkStateProjection().handle(with_ancestors).items}
    assert keys == {"ticket:OMN-23229"}
    # Failing control: the same request without the ancestors is out of scope (the old behaviour).
    without = with_ancestors.model_copy(update={"ancestors": ()})
    assert _scope_of(without, "ticket:OMN-23229") is EnumScopeStatus.PAUSED


def test_an_ancestor_project_scopes_the_chain_below_it(rules, now, tmp_path) -> None:
    snapshot = _chain_snapshot()
    snapshot["ancestors"][1]["projectId"] = FACTORY_ID
    request = _request(rules, now, tmp_path, _overlay(tmp_path), snapshot)
    assert _scope_of(request, "ticket:OMN-23229") is EnumScopeStatus.IN_SCOPE


def test_a_ticket_row_wins_over_an_ancestor_row_of_the_same_id(rules, now) -> None:
    from omnimarket.models.work_state_projection import (
        ModelAncestorSource,
        ModelTicketSource,
    )

    request = ModelWorkStateRequest(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_tickets=(ROOT,)),
        tickets=(
            ModelTicketSource(ticket_id="OMN-1", state_type="started", parent=ROOT),
        ),
        ancestors=(ModelAncestorSource(ticket_id="OMN-1", parent="OMN-9"),),
    )
    assert _scope_of(request, "ticket:OMN-1") is EnumScopeStatus.IN_SCOPE


def test_source_readers_still_name_the_linear_source(rules, now, tmp_path) -> None:
    specs = source_readers(now, tmp_path, {}, None, rules)
    assert EnumWorkStateSource.LINEAR in {s.source for s in specs}


def test_the_chain_is_in_scope_through_the_written_ancestors(
    tmp_path: Path, rules
) -> None:
    """The written file read by the work-state projection: OMN-23229 reaches OMN-22478 through two tickets that are in no open list."""
    run(
        tmp_path,
        Script(page([node(23229, parent=chain(23127, 22880, 22478))])),
        Clock(),
    )
    written = doc(tmp_path)
    assert {i["id"] for i in written["issues"]} == {"OMN-23229"}

    def scope_of(ancestors: bool) -> EnumScopeStatus:
        request = ModelWorkStateRequest(
            now=T0,
            rules=rules,
            scope=ModelWorkScope(in_scope_tickets=("OMN-22478",)),
            tickets=tuple(tickets_from_linear_snapshot(written, rules)),
            ancestors=tuple(ancestors_from_linear_snapshot(written))
            if ancestors
            else (),
        )
        result = HandlerWorkStateProjection().handle(request)
        (item,) = [i for i in result.items if i.key == "ticket:OMN-23229"]
        return item.scope

    assert scope_of(ancestors=False) is EnumScopeStatus.PAUSED
    assert scope_of(ancestors=True) is EnumScopeStatus.IN_SCOPE
