# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures of the merged-change reading effect tests: the contract's own labels and real runtime log lines."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.models.model_merged_change_reading import ModelMcrConfig
from omnimarket.nodes.node_merged_change_reading_effect.models.model_merged_change_reading_run import (
    ModelMcrLogLabels,
)

NODES = Path(__file__).parents[3] / "src/omnimarket/nodes"
EFFECT_CONTRACT = NODES / "node_merged_change_reading_effect/contract.yaml"
COMPUTE_CONTRACT = NODES / "node_merged_change_reading_compute/contract.yaml"

# Lines copied from the dev runtime's log of 2026-11-14 (omnimarket 0.4.321), cut to the fields the patterns read.
LOG_TEXT = "\n".join(
    [
        "2026-11-14 08:57:09 [INFO] omnibase_infra.runtime.auto_wiring.discovery: Discovered contract: node_landing_canary_scheduled_effect (effect) from omnimarket 0.4.321",
        "2026-11-14 08:57:09 [INFO] omnibase_infra.runtime.auto_wiring.discovery: Discovered contract: node_start_path_census_compute (COMPUTE_GENERIC) from omnimarket 0.4.321",
        "2026-11-14 09:11:55 [INFO] aiokafka.consumer.group_coordinator: Setting newly assigned partitions {TopicPartition(topic='onex.intent.platform.runtime-tick.v1', partition=0)} for group local.omnimarket.node_landing_canary_scheduled_effect.consume.0.1.0.__i.runtime-main.__t.onex.intent.platform.runtime-tick.v1",
        "2026-11-14 09:04:08 [INFO] omnibase_infra.runtime.service_dispatch_result_applier: Published output event to onex.evt.omnimarket.schedule-run.v1 (correlation_id=1e76d90a-5830-4194-8ce0-b86bc210f02c)",
        "2026-11-14 09:07:30 [INFO] omnibase_infra.runtime.service_dispatch_result_applier: Published output event to onex.evt.omnimarket.schedule-run.v1 (correlation_id=75fba0af-0000-4000-8000-000000000000)",
        "2026-11-14 09:06:36 [INFO] omnibase_infra.runtime.service_dispatch_result_applier: Published output event to onex.evt.omnimarket.lab-alarm-checked.v1 (correlation_id=3b3b3b3b-0000-4000-8000-000000000000)",
        "2026-11-14 08:57:59 [INFO] omnibase_infra.runtime.auto_wiring.handler_wiring: Auto-wiring (deferred): skipping Kafka subscription for contract 'node_start_path_census_compute' because its wiring outcome is skipped",
    ]
)


def labels() -> ModelMcrLogLabels:
    document = yaml.safe_load(EFFECT_CONTRACT.read_text(encoding="utf-8"))
    return ModelMcrLogLabels.model_validate(document["runtime"]["log"])


def config() -> ModelMcrConfig:
    document = yaml.safe_load(COMPUTE_CONTRACT.read_text(encoding="utf-8"))
    return ModelMcrConfig.model_validate(document["merged_change_reading"])
