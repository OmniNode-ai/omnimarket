# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker topic removal (OMN-23697): the classes and candidates a dry run plans, every typed
refusal an execute run gives, the removal it issues with its switch, its approval and its remover,
and the failing control that proves the protected-prefix refusal is what kept the internal topics out.

The golden and error chain event tests are in test_removal_chain_events.py.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_broker_topic_removal_effect.adapters.declared_topics import (
    InMemoryDeclaredTopics,
)
from omnimarket.nodes.node_broker_topic_removal_effect.handlers.handler_broker_topic_removal import (
    HandlerBrokerTopicRemoval,
)
from omnimarket.nodes.node_broker_topic_removal_effect.models.model_broker_topic_removal import (
    EnumRemovalMode,
    EnumTopicClass,
    ModelTopicObservation,
    ModelTopicRemovalCommand,
    ModelTopicRemovalResult,
)
from omnimarket.nodes.node_broker_topic_removal_effect.overlay import (
    ModelTopicRemovalOverlay,
)
from omnimarket.nodes.node_broker_topic_removal_effect.protocols import (
    ProtocolDeclaredTopics,
    ProtocolTopicInventory,
    ProtocolTopicRemover,
)
from tests.nodes.node_broker_topic_removal_effect.removal_support import (
    OBSERVATIONS,
    TOPICS,
    World,
    overlay,
)

pytestmark = pytest.mark.unit

DRY = ModelTopicRemovalCommand(correlation_id="t-dry")
EXECUTE = ModelTopicRemovalCommand(
    correlation_id="t-exec", mode=EnumRemovalMode.EXECUTE
)
EXECUTE_APPROVED = ModelTopicRemovalCommand(
    correlation_id="t-exec", mode=EnumRemovalMode.EXECUTE, approval_ref="ledger-7"
)
EXECUTE_BLANK_APPROVAL = ModelTopicRemovalCommand(
    correlation_id="t-exec", mode=EnumRemovalMode.EXECUTE, approval_ref="   "
)


def _run(
    world: World, command: ModelTopicRemovalCommand, remover: bool = False
) -> ModelTopicRemovalResult:
    return world.handler(remover=remover).handle(command)


def test_dry_run_lists_only_the_undeclared_never_produced_topic() -> None:
    result = _run(World(), DRY)
    assert result.status == "planned"
    assert result.mode == EnumRemovalMode.DRY_RUN
    assert result.correlation_id == DRY.correlation_id
    assert result.refusal_reasons == ()
    assert [candidate.name for candidate in result.candidates] == [TOPICS["d"]]
    candidate = result.candidates[0]
    assert candidate.partitions == 3
    assert candidate.topic_class is EnumTopicClass.UNDECLARED_NEVER_PRODUCED
    assert candidate.evidence == (
        "no record was ever produced (high watermark total 0)",
        "no data inside retention",
        "no consumer group reads it",
        "no contract declares a producer",
        "no contract declares it",
    )


def test_dry_run_refuses_the_declared_the_grouped_and_the_protected_topics_typed() -> (
    None
):
    result = _run(World(), DRY)
    refused = {entry.reason: entry for entry in result.refused}
    assert set(refused) == {"contract_declared", "has_consumer_group", "protected"}
    assert refused["contract_declared"].topics == 1
    assert refused["contract_declared"].partitions == 1
    assert refused["has_consumer_group"].topics == 1
    assert refused["has_consumer_group"].partitions == 1
    assert refused["protected"].topics == 1
    assert refused["protected"].partitions == 1


def test_dry_run_classes_count_topics_partitions_and_bytes_exactly() -> None:
    result = _run(World(), DRY)
    assert result.topics_before == 6
    assert result.partitions_before == 8
    expected = {
        EnumTopicClass.DECLARED_PRODUCED: (1, 1, 5),
        EnumTopicClass.DECLARED_NEVER_PRODUCED: (1, 1, 0),
        EnumTopicClass.UNDECLARED_PRODUCED: (1, 1, 100),
        EnumTopicClass.UNDECLARED_NEVER_PRODUCED: (3, 5, 0),
    }
    counts = {entry.topic_class: entry for entry in result.classes}
    assert set(counts) == set(expected)
    for cls, (topics, partitions, produced_bytes) in expected.items():
        assert counts[cls].topics == topics, cls
        assert counts[cls].partitions == partitions, cls
        assert counts[cls].produced_bytes == produced_bytes, cls


def test_dry_run_partitions_after_is_before_minus_the_candidate_partitions() -> None:
    result = _run(World(), DRY)
    assert result.candidate_partitions == 3
    assert result.partitions_after == result.partitions_before - 3
    assert result.partitions_after == 5


def test_dry_run_touches_nothing_and_issues_no_deletes() -> None:
    world = World()
    result = _run(world, DRY, remover=True)
    assert result.removed == ()
    assert result.deletes_issued == 0
    assert world.remover.calls == []


def test_with_remove_declared_topics_both_never_produced_topics_are_candidates() -> (
    None
):
    world = World()
    world.break_make_declared_removable()
    result = _run(world, DRY)
    assert [candidate.name for candidate in result.candidates] == [
        TOPICS["c"],
        TOPICS["d"],
    ]
    by_name = {candidate.name: candidate for candidate in result.candidates}
    assert by_name[TOPICS["c"]].topic_class is EnumTopicClass.DECLARED_NEVER_PRODUCED
    assert by_name[TOPICS["c"]].evidence[-2:] == (
        "contract(s) declaring a producer: c2",
        "declared by: c2",
    )
    assert "contract_declared" not in {entry.reason for entry in result.refused}


def test_a_topic_with_data_is_never_a_candidate() -> None:
    result = _run(World(), DRY)
    names = {candidate.name for candidate in result.candidates}
    assert TOPICS["a"] not in names  # high watermark 100
    assert TOPICS["b"] not in names  # high watermark 5


def test_an_unread_watermark_is_never_a_candidate() -> None:
    world = World()
    world.set_observations(
        (
            *OBSERVATIONS,
            ModelTopicObservation(
                name="fixture-topic-f-unread",
                partitions=1,
                high_watermark_total=None,
                consumer_groups=None,
                produced_bytes=None,
            ),
        )
    )
    result = _run(world, DRY)
    names = {candidate.name for candidate in result.candidates}
    assert "fixture-topic-f-unread" not in names


def test_execute_is_refused_with_all_three_reasons_in_order() -> None:
    result = _run(World(), EXECUTE, remover=False)
    assert result.status == "refused"
    assert result.refusal_reasons == (
        "execute_disabled_by_overlay",
        "approval_missing",
        "remover_not_wired",
    )
    assert result.removed == ()
    assert result.deletes_issued == 0


def test_execute_is_refused_approval_missing_on_a_blank_approval() -> None:
    world = World()
    world.enable_execute()
    result = _run(world, EXECUTE_BLANK_APPROVAL, remover=True)
    assert result.status == "refused"
    assert result.refusal_reasons == ("approval_missing",)
    assert world.remover.calls == []


def test_execute_with_switch_approval_and_remover_removes_exactly_its_candidates() -> (
    None
):
    world = World()
    world.enable_execute()
    result = _run(world, EXECUTE_APPROVED, remover=True)
    assert result.status == "removed"
    assert result.refusal_reasons == ()
    names = [candidate.name for candidate in result.candidates]
    assert names == [TOPICS["d"]]
    assert result.removed == tuple(names)
    assert result.deletes_issued == len(names)
    assert world.remover.calls == [tuple(names)]


def test_execute_removes_the_declared_topic_too_when_the_overlay_allows_it() -> None:
    world = World()
    world.enable_execute()
    world.break_make_declared_removable()
    result = _run(world, EXECUTE_APPROVED, remover=True)
    assert result.status == "removed"
    assert result.removed == (TOPICS["c"], TOPICS["d"])
    assert result.deletes_issued == 2
    assert world.remover.calls == [(TOPICS["c"], TOPICS["d"])]


def test_execute_over_the_cap_is_refused_over_run_cap_and_the_remover_saw_nothing() -> (
    None
):
    world = World()
    world.enable_execute()
    world.break_make_declared_removable()
    world.tiny_cap()
    result = _run(world, EXECUTE_APPROVED, remover=True)
    assert result.status == "refused"
    assert result.refusal_reasons == ("over_run_cap",)
    assert result.over_run_cap is True
    assert result.removed == ()
    assert result.deletes_issued == 0
    assert world.remover.calls == []


def test_a_remover_failure_is_refused_remove_failed_and_removes_nothing() -> None:
    world = World()
    world.enable_execute()
    world.break_remover_fails("broker-refused-delete")
    result = _run(world, EXECUTE_APPROVED, remover=True)
    assert result.status == "refused"
    assert result.refusal_reasons == ("remove_failed:broker-refused-delete",)
    assert result.removed == ()
    assert result.deletes_issued == 0


def test_an_unreadable_inventory_is_refused_with_its_typed_reason() -> None:
    world = World()
    world.break_inventory_unreadable()
    result = _run(world, DRY, remover=True)
    assert result.status == "refused"
    assert result.refusal_reasons == ("inventory_unreadable:inventory-unreadable",)
    assert world.remover.calls == []


def test_unreadable_declared_topics_is_refused_with_its_typed_reason() -> None:
    world = World()
    world.break_declared_unreadable()
    result = _run(world, DRY, remover=True)
    assert result.status == "refused"
    assert result.refusal_reasons == (
        "declared_topics_unreadable:registry-root-variable-unset",
    )
    assert world.remover.calls == []


def _overlay_without_protected_prefixes() -> ModelTopicRemovalOverlay:
    base = overlay()
    return base.model_copy(
        update={
            "listing_rule": base.listing_rule.model_copy(
                update={"protected_prefixes": ()}
            )
        }
    )


class _UnprotectedPrefixes(HandlerBrokerTopicRemoval):
    """Overrides nothing but passes an overlay whose rule protects no prefix."""

    def __init__(
        self,
        *,
        inventory: ProtocolTopicInventory,
        declared: ProtocolDeclaredTopics,
        remover: ProtocolTopicRemover | None,
    ) -> None:
        super().__init__(
            overlay=_overlay_without_protected_prefixes(),
            inventory=inventory,
            declared=declared,
            remover=remover,
        )


def test_failing_control_without_protected_prefixes_the_internal_topic_is_listed() -> (
    None
):
    """The prefix refusal is what kept the broker's internal topics out; without it they are listed."""
    world = World()
    handler = _UnprotectedPrefixes(
        inventory=world.inventory, declared=world.declared_topics, remover=None
    )
    result = handler.handle(DRY)
    names = [candidate.name for candidate in result.candidates]
    assert TOPICS["internal"] in names
    assert "protected" not in {entry.reason for entry in result.refused}


def test_failing_control_without_the_group_evidence_the_grouped_topic_is_listed() -> (
    None
):
    """The consumer-group refusal is what kept the grouped never-produced topic out; without its group it is listed."""
    world = World()
    world.set_observations(
        tuple(
            obs.model_copy(update={"consumer_groups": ()})
            if obs.name == TOPICS["e"]
            else obs
            for obs in world.observations or ()
        )
    )
    result = _run(world, DRY)
    assert TOPICS["e"] in [candidate.name for candidate in result.candidates]
    assert "has_consumer_group" not in {entry.reason for entry in result.refused}


def test_failing_control_without_the_contract_declaration_the_declared_topic_is_listed() -> (
    None
):
    """The declaration refusal is what kept the declared never-produced topic out; without it the topic is listed."""
    world = World()
    world.declared_topics = InMemoryDeclaredTopics({}, {})
    result = _run(world, DRY)
    assert TOPICS["c"] in [candidate.name for candidate in result.candidates]
    assert "contract_declared" not in {entry.reason for entry in result.refused}
