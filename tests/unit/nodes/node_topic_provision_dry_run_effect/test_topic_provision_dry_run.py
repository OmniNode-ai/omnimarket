# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dry run counts the live topics under the rule and only ever lists (OMN-19998)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_topic_provision_dry_run_effect.handlers.handler_topic_provision_dry_run import (
    HandlerTopicProvisionDryRun,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.models.model_topic_provision_dry_run_request import (
    ModelTopicProvisionDryRunRequest,
)
from omnimarket.nodes.node_topic_provision_plan_compute.handlers.handler_topic_provision_plan import (
    TopicProvisionRuleError,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.enum_topic_provision import (
    EnumTopicProvisionMode,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_overlay import (
    ModelTopicProvisionOverlay,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_rule import (
    ModelTopicProvisionRule,
)

pytestmark = pytest.mark.unit

RULE = ModelTopicProvisionRule(mode=EnumTopicProvisionMode.PROFILE_AND_MANIFEST)
LIVE = ("topic.a", "topic.c", "topic.z")


class RecordingLister:
    """A fake broker that records every call made to it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_topics(self) -> tuple[str, ...]:
        self.calls.append("list_topics")
        return LIVE


def _write(root: Path, name: str, profiles: list[str], topics: list[str]) -> None:
    node = root / name
    node.mkdir(parents=True)
    (node / "contract.yaml").write_text(
        yaml.safe_dump(
            {
                "contract_name": name,
                "runtime_profiles": profiles,
                "event_bus": {"subscribe_topics": topics},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def contract_root(tmp_path: Path) -> Path:
    _write(tmp_path, "delegation", ["main"], ["topic.a"])
    _write(tmp_path, "board", ["effects"], ["topic.c"])
    return tmp_path


async def test_counts_the_live_topics_under_the_rule(contract_root: Path) -> None:
    lister = RecordingLister()
    overlay = ModelTopicProvisionOverlay(
        profile="main",
        lane_manifest=("delegation",),
        contract_roots=(str(contract_root),),
    )

    plan = await HandlerTopicProvisionDryRun(lister).handle(
        ModelTopicProvisionDryRunRequest(overlay=overlay, rule=RULE)
    )

    assert plan.topics_to_provision == ("topic.a",)
    assert plan.live_topic_count == 3
    assert plan.live_topics_under_rule == 1
    assert plan.live_topics_outside_rule == 2
    assert plan.live_topics_undeclared == 1


async def test_the_dry_run_only_lists(contract_root: Path) -> None:
    lister = RecordingLister()
    overlay = ModelTopicProvisionOverlay(
        profile="main",
        lane_manifest=("delegation",),
        contract_roots=(str(contract_root),),
    )

    await HandlerTopicProvisionDryRun(lister).handle(
        ModelTopicProvisionDryRunRequest(overlay=overlay, rule=RULE)
    )

    assert lister.calls == ["list_topics"]


async def test_refuses_without_a_profile(contract_root: Path) -> None:
    overlay = ModelTopicProvisionOverlay(contract_roots=(str(contract_root),))

    with pytest.raises(TopicProvisionRuleError, match="profile"):
        await HandlerTopicProvisionDryRun(None).handle(
            ModelTopicProvisionDryRunRequest(overlay=overlay, rule=RULE)
        )


async def test_refuses_without_contract_roots() -> None:
    overlay = ModelTopicProvisionOverlay(profile="main")

    with pytest.raises(TopicProvisionRuleError, match="contract_roots"):
        await HandlerTopicProvisionDryRun(None).handle(
            ModelTopicProvisionDryRunRequest(overlay=overlay, rule=RULE)
        )
