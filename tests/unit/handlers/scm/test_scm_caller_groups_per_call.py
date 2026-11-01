# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21818 -- an ``onex scm`` call joins no consumer group.

``ScmBusCaller`` built one ``scm_command_client`` group per call and read the
gateway and write terminals in it: 2,866 Empty groups sat on the dev broker, a
share of the 19,154 that crashed it three times on 2026-10-31. The caller now
reads the terminals with no group.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller, load_topics
from omnimarket.models.scm import EnumScmNode
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
)
from tests.unit.handlers.scm.test_onex_scm_command import FakeBus

pytestmark = pytest.mark.unit

CALLS = 100
#: Groups a hundred scm calls may create in total.
MAX_GROUPS = 1


def test_groups_per_call_a_hundred_scm_calls_create_at_most_one_group() -> None:
    bus = FakeBus()

    async def scenario() -> list[str]:
        outcomes: list[str] = []
        for number in range(CALLS):
            correlation_id = uuid.uuid4()
            terminal = await ScmBusCaller(bus).call(
                node=EnumScmNode.GATEWAY,
                request=ModelGithubGatewayRequest(
                    operation=EnumGithubGatewayOperation.MERGED_PR,
                    repo="OmniNode-ai/omnimarket",
                    pr_number=number + 1,
                    correlation_id=correlation_id,
                ),
                correlation_id=correlation_id,
                timeout_s=5,
            )
            outcomes.append(terminal.receipt.terminal_topic)
        return outcomes

    outcomes = asyncio.run(scenario())

    assert outcomes == [load_topics(EnumScmNode.GATEWAY).success] * CALLS
    assert len(bus.groups) <= MAX_GROUPS
    assert not any("scm_command_client" in group for group in bus.groups)


def test_groups_per_call_a_bus_with_no_ungrouped_read_is_refused() -> None:
    class GroupedOnlyBus:
        async def publish(self, *args: object, **kwargs: object) -> None: ...

        async def subscribe(self, *args: object, **kwargs: object) -> None: ...

    with pytest.raises(TypeError, match="subscribe_ungrouped"):
        ScmBusCaller(cast(Any, GroupedOnlyBus()))


def test_groups_per_call_the_caller_source_mints_no_group_name() -> None:
    source = (
        Path(__file__).resolve().parents[4]
        / "src/omnimarket/handlers/scm/handler_scm_dispatch.py"
    ).read_text(encoding="utf-8")
    caller = source[source.index("class ScmBusCaller") :]
    assert "group_id" not in caller
    assert "scm_command_client" not in caller
    assert "uuid4().hex" not in caller
