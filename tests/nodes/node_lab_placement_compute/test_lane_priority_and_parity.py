# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22124 -- lane priority class from the repository, and the orphan marker in plugin parity.

The fixture ``fixtures/no_host_1102`` holds what the 2026-11-02 incident receipts recorded: a lane on
omnibase_infra charged to ``other`` and refused PRIORITY-HOLDBACK with one free slot held for
mvp-landing; a pinned lane refused PARITY-FAILED for the ``.orphaned_at`` file; and four lanes refused
with no free slot, which are correct refusals and stay the negative controls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_lab_placement_compute.handlers import (
    HandlerLabLanePriority,
    HandlerLabPluginParity,
)
from omnimarket.nodes.node_lab_placement_compute.models import (
    ModelLanePriorityRequest,
    ModelPluginParityRequest,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "no_host_1102" / "incident.json"
INCIDENT: dict[str, Any] = json.loads(FIXTURE.read_text())


def legacy_lane_priority_class(
    handler: HandlerLabLanePriority, request: ModelLanePriorityRequest
) -> str:
    """The rule the runner ran on 2026-11-02: the declared class, else the ticket's class, never the repository."""
    if request.declared in handler.rules.classes:
        return str(request.declared)
    return handler.rules.class_of_ticket(request.ticket)


def _request(case: dict[str, Any]) -> ModelLanePriorityRequest:
    return ModelLanePriorityRequest(
        repo=case["repo"],
        ticket=case["ticket"],
        free_slots=case["pool"]["free_slots"],
        waiting=tuple(case["pool"]["waiting"]),
    )


@pytest.fixture(scope="module")
def priority() -> HandlerLabLanePriority:
    return HandlerLabLanePriority()


@pytest.fixture(scope="module")
def parity() -> HandlerLabPluginParity:
    return HandlerLabPluginParity()


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("omnibase_infra", "mvp-landing"),
        ("OmniNode-ai/omnibase_infra", "mvp-landing"),
        ("omnimarket#4981", "mvp-landing"),
        ("OmniNode-ai/OmniClaude", "mvp-landing"),
        ("some_other_repo", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_priority_class_of_repo_reads_the_contract(
    priority: HandlerLabLanePriority, repo: str | None, expected: str
) -> None:
    assert priority.priority_class_of_repo(repo) == expected


def test_priority_class_of_repo_lane_without_declared_class_takes_the_repository_class(
    priority: HandlerLabLanePriority,
) -> None:
    result = priority.handle(
        ModelLanePriorityRequest(repo="omnibase_infra", ticket="OMN-22124")
    )
    assert (result.priority_class, result.source) == ("mvp-landing", "repository")


def test_priority_class_of_repo_order_declared_then_ticket_then_repository(
    priority: HandlerLabLanePriority,
) -> None:
    # a declared class outranks everything
    declared = priority.handle(
        ModelLanePriorityRequest(
            repo="omnibase_infra", ticket="OMN-20074", declared="process-fix"
        )
    )
    assert (declared.priority_class, declared.source) == ("process-fix", "declared")
    # a name that is no class is no declaration
    junk = priority.handle(
        ModelLanePriorityRequest(repo="omnibase_infra", declared="urgent")
    )
    assert (junk.priority_class, junk.source) == ("mvp-landing", "repository")
    # a ticket the contract names outranks the repository (OCC retirement in an MVP repository)
    ticket = priority.handle(
        ModelLanePriorityRequest(repo="omnibase_infra", ticket="OMN-20074")
    )
    assert (ticket.priority_class, ticket.source) == ("occ-retirement", "ticket")
    # a child of a named parent takes the parent's class
    child = priority.handle(
        ModelLanePriorityRequest(
            repo="omnibase_infra", ticket="OMN-1", parent="omn-20068"
        )
    )
    assert child.priority_class == "occ-retirement"
    # nothing named: default
    none = priority.handle(ModelLanePriorityRequest(repo="some_other_repo"))
    assert (none.priority_class, none.source) == ("other", "default")


@pytest.mark.parametrize("case", INCIDENT["holdback_cases"], ids=lambda c: c["lane"])
def test_priority_class_of_repo_incident_lane_is_placed_where_it_was_held_back(
    priority: HandlerLabLanePriority, case: dict[str, Any]
) -> None:
    request = _request(case)
    # RED: the rule the runner ran charges the lane to other, and the holdback refuses it (the recorded refusal)
    legacy = legacy_lane_priority_class(priority, request)
    assert legacy == case["recorded_priority_class"] == "other"
    held = priority.holdback_refusal(
        legacy, case["pool"]["free_slots"], request.waiting
    )
    assert held is not None
    assert held.startswith(case["recorded_refusal"])
    # GREEN: the repository gives the lane mvp-landing, which nothing holds a slot against
    result = priority.handle(request)
    assert result.priority_class == "mvp-landing"
    assert result.admitted, result.refusal


def test_priority_class_of_repo_holdback_still_holds_the_slot_for_a_lane_of_no_class(
    priority: HandlerLabLanePriority,
) -> None:
    # negative control: the fix moves the lane that belongs to mvp-landing, not the holdback itself
    result = priority.handle(
        ModelLanePriorityRequest(
            repo="some_other_repo", free_slots=1, waiting=("mvp-landing",)
        )
    )
    assert result.priority_class == "other"
    assert result.refusal is not None
    assert result.refusal.startswith("PRIORITY-HOLDBACK")
    # and a class nobody waits for holds nothing
    free = priority.handle(
        ModelLanePriorityRequest(repo="some_other_repo", free_slots=1, waiting=())
    )
    assert free.admitted


@pytest.mark.parametrize(
    "case", INCIDENT["no_slot_negative_controls"], ids=lambda c: c["lane"]
)
@pytest.mark.parametrize("repo", ["omnibase_infra", "some_other_repo"])
def test_priority_class_of_repo_no_free_slot_still_refuses_every_class(
    priority: HandlerLabLanePriority, case: dict[str, Any], repo: str
) -> None:
    result = priority.handle(
        ModelLanePriorityRequest(
            repo=repo, free_slots=case["free_slots"], waiting=("mvp-landing",)
        )
    )
    assert not result.admitted
    assert result.refusal is not None
    assert result.refusal.startswith("NO-FREE-SLOT")


def test_priority_class_of_repo_private_overlay_repositories_are_merged(
    priority: HandlerLabLanePriority,
) -> None:
    private = {"process-fix": ("example_private_repo",)}
    named = priority.handle(
        ModelLanePriorityRequest(repo="example_private_repo", private_repos=private)
    )
    assert (named.priority_class, named.source) == ("process-fix", "repository")
    # without the overlay the contract knows nothing of it
    unnamed = priority.handle(ModelLanePriorityRequest(repo="example_private_repo"))
    assert unnamed.priority_class == "other"
    # an overlay naming a class the contract lacks is refused
    with pytest.raises(ValueError, match="not classes"):
        priority.handle(
            ModelLanePriorityRequest(
                repo="x", private_repos={"made-up": ("example_private_repo",)}
            )
        )


def test_priority_class_of_repo_pool_not_read_judges_only_the_class(
    priority: HandlerLabLanePriority,
) -> None:
    result = priority.handle(ModelLanePriorityRequest(repo="omnibase_infra"))
    assert result.admitted
    assert result.priority_class == "mvp-landing"


SHIPPED = {"skills/a/SKILL.md": "b1", "scripts/run.py": "b2"}


@pytest.mark.parametrize("case", INCIDENT["parity_cases"], ids=lambda c: c["lane"])
def test_orphan_marker_incident_pin_is_not_refused_for_it(
    parity: HandlerLabPluginParity, case: dict[str, Any]
) -> None:
    installed = {**SHIPPED, **dict.fromkeys(case["installed_minus_shipped"], "b9")}
    result = parity.handle(
        ModelPluginParityRequest(installed=installed, shipped=SHIPPED)
    )
    assert result.state == "ok", result.detail
    assert result.ignored == tuple(sorted(case["installed_minus_shipped"]))
    assert result.detail == "identical"


def test_orphan_marker_does_not_hide_a_real_difference(
    parity: HandlerLabPluginParity,
) -> None:
    # negative controls: the marker is left out, every other difference still counts
    added = parity.handle(
        ModelPluginParityRequest(
            installed={**SHIPPED, ".orphaned_at": "b9", "skills/new.md": "b3"},
            shipped=SHIPPED,
        )
    )
    assert (added.state, added.added) == ("modified", ("skills/new.md",))
    changed = parity.handle(
        ModelPluginParityRequest(
            installed={**SHIPPED, ".orphaned_at": "b9", "scripts/run.py": "bX"},
            shipped=SHIPPED,
        )
    )
    assert (changed.state, changed.changed) == ("modified", ("scripts/run.py",))
    removed = parity.handle(
        ModelPluginParityRequest(
            installed={"skills/a/SKILL.md": "b1", ".orphaned_at": "b9"},
            shipped=SHIPPED,
        )
    )
    assert (removed.state, removed.removed) == ("modified", ("scripts/run.py",))


def test_orphan_marker_at_another_depth_still_counts(
    parity: HandlerLabPluginParity,
) -> None:
    nested = parity.handle(
        ModelPluginParityRequest(
            installed={**SHIPPED, "skills/.orphaned_at": "b9"}, shipped=SHIPPED
        )
    )
    assert (nested.state, nested.added) == ("modified", ("skills/.orphaned_at",))


def test_orphan_marker_runtime_marker_directory_is_left_out(
    parity: HandlerLabPluginParity,
) -> None:
    result = parity.handle(
        ModelPluginParityRequest(
            installed={**SHIPPED, ".in_use/4242": "b9"}, shipped=SHIPPED
        )
    )
    assert result.state == "ok"
