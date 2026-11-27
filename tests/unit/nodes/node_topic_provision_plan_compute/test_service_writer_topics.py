# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plan expands each contract's writer_topics template with each declared service login."""

from __future__ import annotations

import pytest

from omnimarket.models.topic_provision.enum_topic_provision import (
    EnumTopicProvisionMode,
)
from omnimarket.models.topic_provision.model_topic_provision_contract import (
    ModelTopicProvisionContract,
)
from omnimarket.models.topic_provision.model_topic_provision_request import (
    ModelTopicProvisionRequest,
)
from omnimarket.models.topic_provision.model_topic_provision_rule import (
    ModelTopicProvisionRule,
)
from omnimarket.models.topic_provision.model_topic_provision_write_grant import (
    ModelTopicProvisionWriteGrant,
)
from omnimarket.models.topic_provision.model_writer_topics_template import (
    ModelWriterTopicsTemplate,
)
from omnimarket.nodes.node_topic_provision_plan_compute.handlers.handler_topic_provision_plan import (
    plan_topic_provision,
)

pytestmark = pytest.mark.unit

DEV_LANE_TOPIC = "onex.cmd.omnimarket.work-ledger-append-from-lane-a.v1"
TEMPLATE = ModelWriterTopicsTemplate(
    kind="cmd",
    producer="omnimarket",
    name_prefix="work-ledger-append-from-",
    version=1,
    principal_pattern="^[a-z0-9][a-z0-9-]{0,62}$",
)
RULE = ModelTopicProvisionRule(mode=EnumTopicProvisionMode.ALL)


def _request(
    *,
    logins: tuple[str, ...] = (),
    grants: tuple[ModelTopicProvisionWriteGrant, ...] = (),
    template: ModelWriterTopicsTemplate | None = TEMPLATE,
) -> ModelTopicProvisionRequest:
    return ModelTopicProvisionRequest(
        contracts=(
            ModelTopicProvisionContract(
                contract_name="node_work_ledger_append_effect",
                runtime_profiles=("effects",),
                topics=("onex.cmd.omnimarket.work-ledger-append-requested.v1",),
                writer_topics=template,
            ),
        ),
        profile="effects",
        rule=RULE,
        service_logins=logins,
        acl_write_grants=grants,
    )


def test_service_writer_login_is_planned_as_its_writer_topic() -> None:
    plan = plan_topic_provision(_request(logins=("lane-a",)))

    assert plan.service_writer_topics == (DEV_LANE_TOPIC,)
    assert DEV_LANE_TOPIC in plan.topics_to_provision


def test_service_writer_login_failing_the_principal_pattern_is_not_planned() -> None:
    plan = plan_topic_provision(_request(logins=("Bad Login", "-lead", "lane-a")))

    assert plan.service_writer_topics == (DEV_LANE_TOPIC,)


def test_service_writer_acl_principal_counts_only_with_write_on_its_writer_topic() -> (
    None
):
    write = ModelTopicProvisionWriteGrant(principal="lane-a", topic=DEV_LANE_TOPIC)
    read_only_elsewhere = ModelTopicProvisionWriteGrant(
        principal="ci-ledger-read",
        topic="onex.evt.omnimarket.work-ledger-append-completed.v1",
    )

    plan = plan_topic_provision(_request(grants=(write, read_only_elsewhere)))

    assert plan.service_writer_topics == (DEV_LANE_TOPIC,)


def test_service_writer_login_in_both_sources_is_planned_once_in_name_order() -> None:
    write = ModelTopicProvisionWriteGrant(principal="lane-a", topic=DEV_LANE_TOPIC)

    plan = plan_topic_provision(
        _request(logins=("zeta-lane", "lane-a"), grants=(write,))
    )

    assert plan.service_writer_topics == (
        DEV_LANE_TOPIC,
        "onex.cmd.omnimarket.work-ledger-append-from-zeta-lane.v1",
    )


def test_service_writer_contract_without_a_template_plans_none() -> None:
    plan = plan_topic_provision(_request(logins=("lane-a",), template=None))

    assert plan.service_writer_topics == ()
    assert DEV_LANE_TOPIC not in plan.topics_to_provision


def test_service_writer_name_is_composed_by_the_one_template_the_serve_uses() -> None:
    assert (
        TEMPLATE.topic_template()
        == "onex.cmd.omnimarket.work-ledger-append-from-{principal}.v1"
    )
    assert TEMPLATE.topic_for("lane-a") == DEV_LANE_TOPIC
    with pytest.raises(ValueError, match="invalid writer principal"):
        TEMPLATE.topic_for("Not Valid")
