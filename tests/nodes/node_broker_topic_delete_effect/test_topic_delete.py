# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker topic delete (OMN-23879): what a report-only run names, the exact typed reason each
held topic is refused under, the delete an enforcing run issues with its approval and its ruling,
and every guard the run refuses on — each asserted by its exact reason string, so removing the
guard fails the test.

The golden and error chain event tests are in test_delete_chain_events.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_broker_topic_delete_effect.adapters.in_memory_admin import (
    InMemoryBrokerTopicAdmin,
)
from omnimarket.nodes.node_broker_topic_delete_effect.models.model_topic_delete import (
    EnumTopicClass,
    EnumTopicDeleteStatus,
    ModelDeleteTarget,
    ModelTopicDeleteCommand,
    ModelTopicDeleteResult,
)
from tests.nodes.node_broker_topic_delete_effect.delete_support import (
    TOPICS,
    World,
)

pytestmark = pytest.mark.unit

APPROVED = "ledger-7"


def _run(world: World, command: ModelTopicDeleteCommand) -> ModelTopicDeleteResult:
    return world.handler().handle(command)


def _entries(result: ModelTopicDeleteResult) -> dict[str, str]:
    """Every named topic's status, keyed back by its fixture name."""
    by_name = {entry.name: entry for entry in result.entries}
    return {
        key: by_name[name].status for key, name in TOPICS.items() if name in by_name
    }


def _reasons(result: ModelTopicDeleteResult, key: str) -> tuple[str, ...]:
    """The refusal reasons of one fixture topic's entry, exactly as the handler gave them."""
    by_name = {entry.name: entry for entry in result.entries}
    return by_name[TOPICS[key]].reasons


def test_report_only_would_delete_only_the_clean_topic_and_calls_no_delete() -> None:
    world = World()
    result = _run(world, world.command(*TOPICS))
    assert result.status == "report_only"
    assert result.correlation_id == "delete-test-1"
    assert result.refusal_reasons == ()
    assert world.admin.calls == []
    statuses = _entries(result)
    assert statuses["clean"] is EnumTopicDeleteStatus.WOULD_DELETE
    assert statuses["absent"] is EnumTopicDeleteStatus.ABSENT
    assert [
        key
        for key, status in statuses.items()
        if status is EnumTopicDeleteStatus.WOULD_DELETE
    ] == ["clean"]


def test_report_only_refuses_every_held_topic_with_its_exact_reasons() -> None:
    world = World()
    result = _run(world, world.command(*TOPICS))
    assert result.status == "report_only"
    assert world.admin.calls == []
    expected = {
        "group": ("consumer_group:g-live",),
        "offsets": ("committed_offsets:g-offsets",),
        "declared": ("declared_not_cleared",),
        "records": ("holds_records",),
        "recent": ("holds_records", "producer_within_window"),
        "old": ("holds_records",),
        "protected": ("protected_prefix",),
    }
    for key, reasons in expected.items():
        assert _reasons(result, key) == reasons, key


def test_enforce_deletes_exactly_the_clean_topic_it_was_given() -> None:
    world = World(enforce=True)
    result = _run(world, world.command("clean", approval_ref=APPROVED))
    assert result.status == "enforced"
    assert result.refusal_reasons == ()
    assert result.failed is False
    assert result.deletes_issued == 1
    assert result.partitions_removed == 1
    entry = next(entry for entry in result.entries if entry.name == TOPICS["clean"])
    assert entry.status is EnumTopicDeleteStatus.DELETED
    assert entry.reasons == ()
    assert world.admin.calls == [(TOPICS["clean"],)]


def test_enforce_deletes_only_what_nothing_refuses() -> None:
    world = World(enforce=True)
    result = _run(world, world.command("clean", "group", approval_ref=APPROVED))
    assert result.status == "enforced"
    assert _entries(result) == {
        "clean": EnumTopicDeleteStatus.DELETED,
        "group": EnumTopicDeleteStatus.REFUSED,
    }
    assert _reasons(result, "group") == ("consumer_group:g-live",)
    assert world.admin.calls == [(TOPICS["clean"],)]
    assert TOPICS["group"] not in world.admin.calls[0]


def test_enforce_without_an_approval_is_refused_approval_missing() -> None:
    world = World(enforce=True)
    for approval in (None, "   "):
        result = _run(world, world.command("clean", approval_ref=approval))
        assert result.status == "refused", approval
        assert result.refusal_reasons == ("approval_missing",), approval
        assert world.admin.calls == []


def test_a_command_over_the_cap_is_refused_over_command_cap() -> None:
    world = World(enforce=True)
    command = ModelTopicDeleteCommand(
        correlation_id="delete-test-1",
        topics=tuple(
            ModelDeleteTarget(
                name=f"fixture-over-cap-{index:02d}",
                topic_class=EnumTopicClass.UNDECLARED_NEVER_PRODUCED,
            )
            for index in range(world.overlay.max_topics_per_command + 1)
        ),
        approval_ref=APPROVED,
    )
    result = _run(world, command)
    assert result.status == "refused"
    assert result.refusal_reasons == ("over_command_cap",)
    assert world.admin.calls == []


def test_a_declared_topic_without_a_ruling_is_refused_declared_not_cleared() -> None:
    world = World(enforce=True)
    result = _run(world, world.command("declared", approval_ref=APPROVED))
    assert result.status == "enforced"
    assert _reasons(result, "declared") == ("declared_not_cleared",)
    assert world.admin.calls == []


def test_a_ruled_declared_topic_is_deleted() -> None:
    world = World(enforce=True)
    result = _run(
        world,
        world.command(
            "declared",
            approval_ref=APPROVED,
            ruling_covers_declared=True,
            ruling_ref="ruling-9",
        ),
    )
    assert result.status == "enforced"
    entry = next(entry for entry in result.entries if entry.name == TOPICS["declared"])
    assert entry.status is EnumTopicDeleteStatus.DELETED
    assert entry.reasons == ()
    assert world.admin.calls == [(TOPICS["declared"],)]


def test_a_declared_topic_named_undeclared_is_refused_declared_contradicts_command() -> (
    None
):
    world = World()
    command = ModelTopicDeleteCommand(
        correlation_id="delete-test-1",
        topics=(
            ModelDeleteTarget(
                name=TOPICS["declared"],
                topic_class=EnumTopicClass.UNDECLARED_NEVER_PRODUCED,
            ),
        ),
    )
    result = _run(world, command)
    assert result.status == "report_only"
    assert _reasons(result, "declared") == ("declared_contradicts_command",)
    assert world.admin.calls == []


def test_a_producer_inside_the_window_is_refused_producer_within_window() -> None:
    world = World(enforce=True)
    result = _run(world, world.command("recent", approval_ref=APPROVED))
    assert result.status == "enforced"
    assert _reasons(result, "recent") == (
        "holds_records",
        "producer_within_window",
    )
    assert world.admin.calls == []


def test_a_producer_older_than_the_window_passes_that_guard() -> None:
    world = World()
    result = _run(world, world.command("old"))
    assert result.status == "report_only"
    assert "producer_within_window" not in _reasons(result, "old")
    assert _reasons(result, "old") == ("holds_records",)


def test_a_delete_error_fails_that_topic_delete_failed() -> None:
    world = World(enforce=True)
    world.break_delete("clean", "topic-marked-for-deletion")
    result = _run(world, world.command("clean", approval_ref=APPROVED))
    assert result.status == "enforced"
    assert result.failed is True
    assert _entries(result) == {"clean": EnumTopicDeleteStatus.FAILED}
    assert _reasons(result, "clean") == ("delete_failed:topic-marked-for-deletion",)
    assert world.admin.calls == [(TOPICS["clean"],)]


def test_a_topic_still_present_after_delete_fails_still_present() -> None:
    class _KeepsTopics(InMemoryBrokerTopicAdmin):
        """The broker answers success yet keeps the topic, as a lagging broker can."""

        def delete(self, names: Sequence[str]) -> dict[str, str]:
            self.calls.append(tuple(names))
            return dict.fromkeys(names, "")

    world = World(enforce=True)
    world.admin = _KeepsTopics(world.evidence)
    result = _run(world, world.command("clean", approval_ref=APPROVED))
    assert result.status == "enforced"
    assert result.failed is True
    assert _entries(result) == {"clean": EnumTopicDeleteStatus.FAILED}
    assert _reasons(result, "clean") == ("still_present",)
    assert world.admin.calls == [(TOPICS["clean"],)]


def test_an_unreadable_broker_is_refused_broker_unreadable() -> None:
    world = World()
    world.break_broker_unreadable()
    result = _run(world, world.command("clean"))
    assert result.status == "refused"
    assert result.refusal_reasons == ("broker_unreadable:broker-unreadable",)
    assert world.admin.calls == []


def test_unreadable_contracts_are_refused_declared_topics_unreadable() -> None:
    world = World()
    world.break_declared_unreadable()
    result = _run(world, world.command("clean"))
    assert result.status == "refused"
    assert result.refusal_reasons == ("declared_topics_unreadable:declared-unreadable",)
    assert world.admin.calls == []


def test_the_command_rejects_duplicate_topic_names() -> None:
    target = ModelDeleteTarget(
        name=TOPICS["clean"], topic_class=EnumTopicClass.UNDECLARED_NEVER_PRODUCED
    )
    with pytest.raises(ValidationError, match="duplicate topic names"):
        ModelTopicDeleteCommand(correlation_id="delete-test-1", topics=(target, target))
