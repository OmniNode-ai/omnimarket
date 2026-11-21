# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression: the dev ledger append topic was declared by a template and never provisioned.

Built from the recorded data of the incident, as the fixture holds it:
tests/fixtures/topic_provision/work_ledger_writer_incident/ (the ledger node contract's writer_topics
template and the rows of a synthetic login (lane-a) in the service writers overlay and the dev-lane scope overlay).
On the dev broker the topic was absent (auto create off), so the runtime's append failed and its restart was held.
"""

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
from omnimarket.nodes.node_topic_provision_dry_run_effect.handlers.handler_topic_provision_dry_run import (
    HandlerTopicProvisionDryRun,
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


class AbsentBroker:
    def __init__(self) -> None:
        self.live: list[str] = ["onex.evt.omnimarket.work-ledger-append-completed.v1"]
        self.creates: list[tuple[str, int, int]] = []

    async def list_topics(self) -> tuple[str, ...]:
        return tuple(sorted(self.live))

    async def create_topic(
        self, name: str, num_partitions: int, replication_factor: int
    ) -> bool:
        self.creates.append((name, num_partitions, replication_factor))
        self.live.append(name)
        return True


async def test_the_dev_ledger_append_topic_is_planned_and_created_from_the_recorded_declarations(
    tmp_path: Path,
) -> None:
    node = tmp_path / "contracts" / "node_work_ledger_append_effect"
    node.mkdir(parents=True)
    shutil.copy(
        FIXTURE / "ledger_append_contract_template.yaml", node / "contract.yaml"
    )
    writers = tmp_path / "overlays" / "node_work_ledger_append_effect"
    writers.mkdir(parents=True)
    shutil.copy(FIXTURE / "service_writers_overlay.yaml", writers / "overlay.yaml")
    scope = tmp_path / "overlays" / "broker_acls_dev_runtime_ledger_append"
    scope.mkdir()
    shutil.copy(FIXTURE / "broker_acls_scope_overlay.yaml", scope / "overlay.yaml")
    request = ModelTopicProvisionDryRunRequest(
        overlay=ModelTopicProvisionOverlay(
            profile="effects",
            contract_roots=(str(tmp_path / "contracts"),),
            overlay_roots=(str(tmp_path / "overlays"),),
        ),
        rule=ModelTopicProvisionRule(mode=EnumTopicProvisionMode.ALL),
    )
    broker = AbsentBroker()

    dry = await HandlerTopicProvisionDryRun(broker).handle(request)
    assert dry.service_writer_topics == (DEV_LANE_TOPIC,)
    assert broker.creates == []

    applied = await HandlerWriterTopicApply(broker, broker).handle(request)

    assert applied.created == (DEV_LANE_TOPIC,)
    assert broker.creates == [(DEV_LANE_TOPIC, 1, 1)]
    assert DEV_LANE_TOPIC in await broker.list_topics()
