# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The writer's broker grants cover every topic it publishes to (OMN-26708).

The writer republishes each row to two snapshot exposures and routes a
malformed receipt to the dead-letter topic. The runtime principal needs WRITE
and DESCRIBE on each, beside READ on the transition topic, or the composed
runtime's writer cannot publish what it projects.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_lane_work import contract_topics

pytestmark = pytest.mark.unit


def _grants() -> set[tuple[str, str]]:
    contract = yaml.safe_load(Path(contract_topics.CONTRACT_PATH).read_text("utf-8"))
    out: set[tuple[str, str]] = set()
    for entry in contract["broker_acl_grants"]:
        for grant in entry["grants"]:
            out.add((grant["name"], grant["operation"]))
    return out


@pytest.mark.parametrize(
    "topic",
    [
        contract_topics.TOPIC_EXPOSURE_WORKFLOW,
        contract_topics.TOPIC_EXPOSURE_TERMINAL,
        "onex.dlq.omnimarket.projection-lane-work-malformed.v1",
    ],
)
@pytest.mark.parametrize("operation", ["WRITE", "DESCRIBE"])
def test_the_runtime_principal_may_publish_every_writer_topic(
    topic: str, operation: str
) -> None:
    assert (topic, operation) in _grants()
