# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The writer topic apply creates each planned writer topic that is absent, once."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnimarket.models.topic_provision.enum_topic_provision import (
    EnumTopicProvisionMode,
)
from omnimarket.models.topic_provision.model_topic_provision_overlay import (
    ModelTopicProvisionOverlay,
)
from omnimarket.models.topic_provision.model_topic_provision_rule import (
    ModelTopicProvisionRule,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.handlers.handler_writer_topic_apply import (
    HandlerWriterTopicApply,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.models.model_topic_provision_dry_run_request import (
    ModelTopicProvisionDryRunRequest,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parents[3]
    / "fixtures"
    / "topic_provision"
    / "work_ledger_writer_incident"
)
DEV_LANE_TOPIC = "onex.cmd.omnimarket.work-ledger-append-from-lane-a.v1"
RULE = ModelTopicProvisionRule(mode=EnumTopicProvisionMode.ALL)


class FakeBroker:
    """A broker that lists what it holds and records every create call."""

    def __init__(self, live: tuple[str, ...] = ()) -> None:
        self.live = list(live)
        self.creates: list[tuple[str, int, int]] = []

    async def list_topics(self) -> tuple[str, ...]:
        return tuple(sorted(self.live))

    async def create_topic(
        self, name: str, num_partitions: int, replication_factor: int
    ) -> bool:
        self.creates.append((name, num_partitions, replication_factor))
        if name in self.live:
            return False
        self.live.append(name)
        return True


@pytest.fixture
def request_with_declarations(tmp_path: Path) -> ModelTopicProvisionDryRunRequest:
    contracts = tmp_path / "contracts" / "node_work_ledger_append_effect"
    contracts.mkdir(parents=True)
    shutil.copy(
        FIXTURE / "ledger_append_contract_template.yaml", contracts / "contract.yaml"
    )
    overlays = tmp_path / "overlays"
    (overlays / "node_work_ledger_append_effect").mkdir(parents=True)
    shutil.copy(
        FIXTURE / "service_writers_overlay.yaml",
        overlays / "node_work_ledger_append_effect" / "overlay.yaml",
    )
    (overlays / "broker_acls_dev_runtime_ledger_append").mkdir()
    shutil.copy(
        FIXTURE / "broker_acls_scope_overlay.yaml",
        overlays / "broker_acls_dev_runtime_ledger_append" / "overlay.yaml",
    )
    return ModelTopicProvisionDryRunRequest(
        overlay=ModelTopicProvisionOverlay(
            profile="effects",
            contract_roots=(str(tmp_path / "contracts"),),
            overlay_roots=(str(overlays),),
        ),
        rule=RULE,
    )


async def test_writer_topic_apply_creates_the_absent_topic_with_one_partition(
    request_with_declarations: ModelTopicProvisionDryRunRequest,
) -> None:
    broker = FakeBroker()

    result = await HandlerWriterTopicApply(broker, broker).handle(
        request_with_declarations
    )

    assert result.planned == (DEV_LANE_TOPIC,)
    assert result.created == (DEV_LANE_TOPIC,)
    assert broker.creates == [(DEV_LANE_TOPIC, 1, 1)]


async def test_writer_topic_apply_second_run_issues_no_create(
    request_with_declarations: ModelTopicProvisionDryRunRequest,
) -> None:
    broker = FakeBroker()
    handler = HandlerWriterTopicApply(broker, broker)

    await handler.handle(request_with_declarations)
    broker.creates.clear()
    second = await handler.handle(request_with_declarations)

    assert broker.creates == []
    assert second.created == ()
    assert second.existing == (DEV_LANE_TOPIC,)


async def test_writer_topic_apply_accepts_a_topic_that_appears_between_list_and_create(
    request_with_declarations: ModelTopicProvisionDryRunRequest,
) -> None:
    class Racing(FakeBroker):
        async def list_topics(self) -> tuple[str, ...]:
            return ()

    broker = Racing(live=(DEV_LANE_TOPIC,))

    result = await HandlerWriterTopicApply(broker, broker).handle(
        request_with_declarations
    )

    assert result.created == ()
    assert result.existing == (DEV_LANE_TOPIC,)
