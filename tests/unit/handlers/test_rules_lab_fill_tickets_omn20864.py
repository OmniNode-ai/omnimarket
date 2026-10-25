# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20864: sprint backlog fills idle slots under the 2026-10-20 ruling."""

from datetime import date

import pytest
from pydantic import ValidationError

from omnimarket.handlers.rules_lab_fill_tickets import (
    drop_line,
    parse_criteria,
    plan_ticket_backlog,
    sprint_window,
)
from omnimarket.models.lab_fill import (
    EnumLabFillTicketDrop,
    EnumLabFillTicketSource,
    ModelLabFillTicketDrop,
    ModelLabFillTicketFacts,
    ModelLabFillTicketIssue,
    ModelLabFillTicketItem,
    ModelLabFillTicketPlan,
)

SPRINT = "Sprint 2026-10-26 → 2026-11-01: M4 End-to-End Part 4"


def _issue(ticket: str = "OMN-20864", **changes: object) -> ModelLabFillTicketIssue:
    return ModelLabFillTicketIssue.model_validate(
        {
            "id": ticket,
            "title": "Fill idle lab slots",
            "status": "Todo",
            "project": SPRINT,
            "repo": "omnimarket",
            "description": "- [ ] AC1: x",
            **changes,
        }
    )


def _facts(
    *issues: ModelLabFillTicketIssue, **changes: object
) -> ModelLabFillTicketFacts:
    return ModelLabFillTicketFacts.model_validate(
        {"today": "2026-10-25", "issues": issues, **changes}
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("today", "source"),
    [
        ("2026-10-25", EnumLabFillTicketSource.SPRINT_NEXT),
        ("2026-10-27", EnumLabFillTicketSource.SPRINT_CURRENT),
    ],
)
def test_open_unassigned_sprint_ticket(
    today: str, source: EnumLabFillTicketSource
) -> None:
    issue = _issue()
    plan = plan_ticket_backlog(_facts(issue, today=today))
    assert not plan.drops
    assert plan.items == (
        ModelLabFillTicketItem(
            id=issue.id,
            title=issue.title,
            repo=issue.repo,
            status=issue.status,
            source=source,
            project=SPRINT,
            criteria=("AC1",),
        ),
    )


@pytest.mark.unit
def test_distant_sprint_is_counted_as_not_pillar() -> None:
    plan = plan_ticket_backlog(_facts(_issue(), today="2026-10-10"))
    assert not plan.items
    assert plan.drops == (
        ModelLabFillTicketDrop(
            id="OMN-20864", reason=EnumLabFillTicketDrop.NOT_PILLAR, detail=SPRINT
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize("assignee", [None, "", "me", "operator"])
def test_unassigned_or_operator_owned_sprint_ticket(assignee: str | None) -> None:
    plan = plan_ticket_backlog(_facts(_issue(assignee=assignee), operator="operator"))
    assert len(plan.items) == 1
    assert not plan.drops


@pytest.mark.unit
def test_other_owner_names_assignee() -> None:
    plan = plan_ticket_backlog(
        _facts(_issue(assignee="someone-else"), operator="operator")
    )
    assert not plan.items
    assert plan.drops[0].reason is EnumLabFillTicketDrop.OTHER_OWNER
    assert plan.drops[0].detail == "someone-else"


@pytest.mark.unit
@pytest.mark.parametrize(
    "description", ["", "- [x] AC1: done", "* [X] **AC-M1**: done"]
)
def test_sprint_requires_unchecked_criteria(description: str) -> None:
    plan = plan_ticket_backlog(_facts(_issue(description=description)))
    assert not plan.items
    assert plan.drops[0].reason is EnumLabFillTicketDrop.NO_CRITERIA


@pytest.mark.unit
@pytest.mark.parametrize(
    ("changes", "fact_changes", "reason"),
    [
        ({"status": "Done"}, {}, EnumLabFillTicketDrop.CLOSED),
        ({"status": "CANCELED"}, {}, EnumLabFillTicketDrop.CLOSED),
        ({"status": "cancelled"}, {}, EnumLabFillTicketDrop.CLOSED),
        ({"status": "Duplicate"}, {}, EnumLabFillTicketDrop.CLOSED),
        ({"status_type": "completed"}, {}, EnumLabFillTicketDrop.CLOSED),
        ({"status_type": "canceled"}, {}, EnumLabFillTicketDrop.CLOSED),
        ({"status_type": "cancelled"}, {}, EnumLabFillTicketDrop.CLOSED),
        ({}, {"claimed": ("OMN-20864",)}, EnumLabFillTicketDrop.CLAIMED),
        ({}, {"open_pr": ("OMN-20864",)}, EnumLabFillTicketDrop.OPEN_PR),
        (
            {"repo": "m4_out"},
            {"out_of_scope": ("m4_out",)},
            EnumLabFillTicketDrop.OUT_OF_SCOPE,
        ),
        (
            {"repo": "other_repo"},
            {"out_of_scope": ("other_repo",)},
            EnumLabFillTicketDrop.OUT_OF_SCOPE,
        ),
        (
            {"title": "Fix OTHER_REPO integration"},
            {"out_of_scope": ("other_repo",)},
            EnumLabFillTicketDrop.OUT_OF_SCOPE,
        ),
        (
            {"labels": ("m4_out",)},
            {"out_of_scope": ("m4_out",)},
            EnumLabFillTicketDrop.OUT_OF_SCOPE,
        ),
        (
            {"project": SPRINT + " (m4_out)"},
            {"out_of_scope": ("m4_out",)},
            EnumLabFillTicketDrop.OUT_OF_SCOPE,
        ),
        ({"title": "Improve Dashboard layout"}, {}, EnumLabFillTicketDrop.DASHBOARD),
        ({"id": "OMN-19969"}, {}, EnumLabFillTicketDrop.DASHBOARD),
        ({"repo": "omnidash"}, {}, EnumLabFillTicketDrop.DASHBOARD),
        ({"labels": ("OmniDash UI",)}, {}, EnumLabFillTicketDrop.DASHBOARD),
        ({}, {"m4_carriers": {"C31": ("OMN-20864",)}}, EnumLabFillTicketDrop.DASHBOARD),
        ({}, {"m4_carriers": {"C32": ("OMN-20864",)}}, EnumLabFillTicketDrop.DASHBOARD),
    ],
)
def test_exclusion_gates(
    changes: dict[str, object],
    fact_changes: dict[str, object],
    reason: EnumLabFillTicketDrop,
) -> None:
    plan = plan_ticket_backlog(_facts(_issue(**changes), **fact_changes))
    assert not plan.items
    assert plan.drops[0].reason is reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "title",
    ["other_repo_extra", "my-other_repo", "other_repo-suffix", "prefixother_repo"],
)
def test_excluded_repo_text_requires_whole_word(title: str) -> None:
    plan = plan_ticket_backlog(
        _facts(_issue(title=title), out_of_scope=("other_repo",))
    )
    assert len(plan.items) == 1
    assert not plan.drops


@pytest.mark.unit
def test_excluded_repo_names_are_regex_literals() -> None:
    plan = plan_ticket_backlog(
        _facts(
            _issue("OMN-1", title="Fix repo.+ integration"),
            _issue("OMN-2", title="Fix repository integration"),
            out_of_scope=("repo.+",),
        )
    )
    assert [item.id for item in plan.items] == ["OMN-2"]
    assert plan.drops[0].id == "OMN-1"
    assert plan.drops[0].reason is EnumLabFillTicketDrop.OUT_OF_SCOPE


@pytest.mark.unit
def test_existing_sources_do_not_require_criteria_and_carriers_take_priority() -> None:
    plan = plan_ticket_backlog(
        _facts(
            _issue(
                "OMN-1", assignee="operator", description="", labels=("delegation",)
            ),
            _issue(
                "OMN-2", assignee="operator", description="", labels=("Delegation:P1",)
            ),
            operator="operator",
            m4_carriers={"C9": ("OMN-1",), "C1": ("OMN-1",)},
        )
    )
    assert not plan.drops
    assert [item.source for item in plan.items] == [
        EnumLabFillTicketSource.M4_CARRIER,
        EnumLabFillTicketSource.DELEGATION,
    ]
    assert plan.items[0].carriers == ("C1", "C9")
    assert all(item.criteria == () for item in plan.items)


@pytest.mark.unit
@pytest.mark.parametrize(
    "changes",
    [
        {"pillar": "delegation"},
        {"project": "Delegation work"},
        {"labels": ("DELEGATION",)},
    ],
)
def test_delegation_sources(changes: dict[str, object]) -> None:
    plan = plan_ticket_backlog(_facts(_issue(description="", **changes)))
    assert not plan.drops
    assert plan.items[0].source is EnumLabFillTicketSource.DELEGATION


@pytest.mark.unit
def test_source_ranking_is_stable_and_drops_keep_input_order() -> None:
    current = "Sprint 2026-10-19 -> 2026-10-25: current"
    issues = (
        _issue("OMN-1"),
        _issue("OMN-2", project="Other project"),
        _issue("OMN-3", project=current),
        _issue("OMN-4", pillar="delegation"),
        _issue("OMN-5", project=""),
        _issue("OMN-6"),
        _issue("OMN-7", project=current),
        _issue("OMN-8", labels=("delegation",)),
        _issue("OMN-9"),
        _issue("OMN-10"),
    )
    facts = _facts(*issues, m4_carriers={"C1": ("OMN-9", "OMN-6")})
    before = facts.model_dump()
    plan = plan_ticket_backlog(facts)
    assert [item.id for item in plan.items] == [
        "OMN-6",
        "OMN-9",
        "OMN-4",
        "OMN-8",
        "OMN-3",
        "OMN-7",
        "OMN-1",
        "OMN-10",
    ]
    assert [drop.id for drop in plan.drops] == ["OMN-2", "OMN-5"]
    assert [drop.detail for drop in plan.drops] == ["Other project", "no project"]
    assert all(drop.reason is EnumLabFillTicketDrop.NOT_PILLAR for drop in plan.drops)
    assert len(plan.items) + len(plan.drops) == len(issues)
    assert facts.model_dump() == before
    assert plan_ticket_backlog(facts) == plan


@pytest.mark.unit
def test_first_matching_gate_wins() -> None:
    plan = plan_ticket_backlog(
        _facts(
            _issue("OMN-1", status="Done", assignee="other"),
            _issue("OMN-2", assignee="other", project="Other project"),
            _issue("OMN-3", project="Other project", repo="m4_out"),
            _issue("OMN-4", repo="m4_out", title="Dashboard"),
            _issue("OMN-5", title="Dashboard"),
            _issue("OMN-6", description=""),
            _issue("OMN-7", description=""),
            open_pr=("OMN-5", "OMN-6"),
            claimed=("OMN-6", "OMN-7"),
            out_of_scope=("m4_out",),
        )
    )
    assert not plan.items
    assert [drop.reason for drop in plan.drops] == [
        EnumLabFillTicketDrop.CLOSED,
        EnumLabFillTicketDrop.OTHER_OWNER,
        EnumLabFillTicketDrop.NOT_PILLAR,
        EnumLabFillTicketDrop.OUT_OF_SCOPE,
        EnumLabFillTicketDrop.DASHBOARD,
        EnumLabFillTicketDrop.OPEN_PR,
        EnumLabFillTicketDrop.CLAIMED,
    ]


@pytest.mark.unit
def test_drop_counts_and_line() -> None:
    plan = plan_ticket_backlog(
        _facts(
            _issue("OMN-1", description=""),
            _issue("OMN-2", status="Done"),
            _issue("OMN-3"),
            _issue("OMN-4", status_type="canceled"),
            _issue("OMN-5", project="Other project"),
        )
    )
    counts = plan.drop_counts()
    assert counts == {"closed": 2, "no-criteria": 1, "not-pillar": 1}
    assert list(counts) == sorted(counts)
    assert drop_line(plan) == "tickets=1 dropped: closed=2, no-criteria=1, not-pillar=1"
    assert drop_line(plan_ticket_backlog(_facts(_issue()))) == "tickets=1 dropped: none"
    assert drop_line(ModelLabFillTicketPlan()) == "tickets=0 dropped: none"


@pytest.mark.unit
@pytest.mark.parametrize("separator", ["→", "->", "to"])
@pytest.mark.parametrize(
    ("today", "source"),
    [
        (date(2026, 10, 18), None),
        (date(2026, 10, 19), EnumLabFillTicketSource.SPRINT_NEXT),
        (date(2026, 10, 26), EnumLabFillTicketSource.SPRINT_CURRENT),
        (date(2026, 11, 1), EnumLabFillTicketSource.SPRINT_CURRENT),
        (date(2026, 11, 2), None),
    ],
)
def test_sprint_window_boundaries(
    separator: str, today: date, source: EnumLabFillTicketSource | None
) -> None:
    assert sprint_window(SPRINT.replace("→", separator), today) is source


@pytest.mark.unit
@pytest.mark.parametrize(
    "project",
    [
        "",
        "M4 End-to-End Part 4",
        "Sprint 2026-99-26 → 2026-11-01: invalid start",
        "Sprint 2026-10-26 → 2026-11-99: invalid end",
        "Sprint 2026-11-01 → 2026-10-26: reversed",
        "Sprint 2026-10-26 → 2026-11-010: malformed",
    ],
)
def test_sprint_window_unparseable_or_reversed(project: str) -> None:
    assert sprint_window(project, date(2026, 10, 27)) is None


@pytest.mark.unit
def test_parse_criteria_preserves_order_and_deduplicates() -> None:
    description = """Acceptance criteria:
- [ ] AC1: text
* [ ] **AC-M1**: text
  - [ ] AC12: text
- [ ] D3: text
* [ ] R8: text
- [ ] AC1: repeated
- [x] AC2: checked
* [X] **AC-M2**: checked
- [ ] AC: no digit
- [ ] Unlabelled text
AC9: no checkbox
- [ ] AC3 missing colon
- [ ] **AC4: unmatched bold
"""
    assert parse_criteria(description) == ("AC1", "AC-M1", "AC12", "D3", "R8")
    assert parse_criteria("") == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "model",
    [
        ModelLabFillTicketIssue(id="OMN-1"),
        ModelLabFillTicketFacts(today="2026-10-25"),
        ModelLabFillTicketItem(
            id="OMN-1",
            title="",
            repo="",
            status="",
            source=EnumLabFillTicketSource.SPRINT_NEXT,
        ),
        ModelLabFillTicketDrop(id="OMN-1", reason=EnumLabFillTicketDrop.CLOSED),
        ModelLabFillTicketPlan(),
    ],
)
def test_contracts_are_frozen_and_forbid_extra(
    model: ModelLabFillTicketIssue
    | ModelLabFillTicketFacts
    | ModelLabFillTicketItem
    | ModelLabFillTicketDrop
    | ModelLabFillTicketPlan,
) -> None:
    with pytest.raises(ValidationError, match="frozen_instance"):
        model.__setattr__(next(iter(type(model).model_fields)), "changed")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        type(model).model_validate({**model.model_dump(), "unexpected": True})


@pytest.mark.unit
def test_contract_patterns() -> None:
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        ModelLabFillTicketIssue(id="other-1")
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        ModelLabFillTicketFacts(today="2026-1-1")
