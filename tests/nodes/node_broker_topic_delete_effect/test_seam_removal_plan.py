# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam with node_broker_topic_removal_effect (OMN-23879): a real plan the removal handler
returns becomes delete targets and a command the delete node validates, and the topic classes the
two nodes share are the four both sides' docs name.

The command carries no planning of its own, so the seam is exactly the candidates a dry run of the
removal node returns, re-named one for one.
"""

from __future__ import annotations

import pytest

from omnimarket.events.topic_removal import (
    EnumRemovalMode,
    EnumTopicClass,
    ModelTopicRemovalCommand,
)
from omnimarket.nodes.node_broker_topic_delete_effect.models.model_topic_delete import (
    ModelDeleteTarget,
    ModelTopicDeleteCommand,
)
from omnimarket.nodes.node_broker_topic_removal_effect.handlers.handler_broker_topic_removal import (
    load_topics as load_removal_topics,
)
from tests.nodes.node_broker_topic_removal_effect.removal_support import TOPICS, World

pytestmark = pytest.mark.unit

#: Classes the removal plan can give a topic; the delete command re-states them, so a rename on
#: either side must fail here.
EXPECTED_CLASSES = {
    "declared_produced",
    "declared_never_produced",
    "undeclared_produced",
    "undeclared_never_produced",
}


def test_removal_plan_candidates_validate_as_delete_command() -> None:
    """Every candidate a real plan returns maps one for one onto a delete command that validates."""
    world = World()
    world.break_make_declared_removable()
    result = world.handler().handle(
        ModelTopicRemovalCommand(
            correlation_id="seam-dry", mode=EnumRemovalMode.DRY_RUN
        )
    )
    assert result.status == "planned"
    # Both never-produced classes appear: the declared one only because the overlay allows it.
    assert [candidate.name for candidate in result.candidates] == [
        TOPICS["c"],
        TOPICS["d"],
    ]
    by_name = {candidate.name: candidate for candidate in result.candidates}
    assert by_name[TOPICS["c"]].topic_class is EnumTopicClass.DECLARED_NEVER_PRODUCED
    assert by_name[TOPICS["d"]].topic_class is EnumTopicClass.UNDECLARED_NEVER_PRODUCED

    targets = tuple(
        ModelDeleteTarget(name=candidate.name, topic_class=candidate.topic_class)
        for candidate in result.candidates
    )
    command = ModelTopicDeleteCommand(
        correlation_id=result.correlation_id, topics=targets
    )
    assert command.correlation_id == result.correlation_id
    assert command.topics == targets
    assert [target.name for target in command.topics] == [
        candidate.name for candidate in result.candidates
    ]
    assert [target.topic_class for target in command.topics] == [
        candidate.topic_class for candidate in result.candidates
    ]
    # The command round-trips the frozen models it was built from.
    assert (
        ModelTopicDeleteCommand.model_validate(command.model_dump(mode="python"))
        == command
    )


def test_topic_classes_are_the_four_both_nodes_document() -> None:
    """The shared enum holds exactly the four classes; a change on either side fails here."""
    assert {member.value for member in EnumTopicClass} == EXPECTED_CLASSES


def test_the_removal_plan_ends_on_the_topic_this_seam_reads_the_plan_from() -> None:
    """The removal node's terminal event is the run-completed topic its plan chain ends on."""
    assert (
        load_removal_topics()["broker_topic_removal_run_completed"]
        == "onex.evt.omnimarket.broker-topic-removal-run-completed.v1"
    )
