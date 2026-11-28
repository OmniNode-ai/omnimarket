# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live-record age bound applies to an overlay the loader asserts, not to one only the store holds.

The loader asserts the settings on its list each hour, so an old record of one of those means the read missed the
newest. An overlay the loader does not list is written only by a set, so its record is as old as the last set and
the bound would refuse every later set after two idle hours. The store's revision check still refuses a write built
on a replaced record, for both kinds.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    LiveRecordStaleError,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from tests.unit.nodes.node_overlay_setting_set.support import (
    ACTOR,
    BOOT_SET,
    KEY,
    NOW,
    OVERLAY_ID,
    build,
    contract,
    live_record,
)
from tests.unit.nodes.node_overlay_setting_set.test_setting_set_goal_scope_ticket_rule import (
    OVERLAY_ID as RULE_ID,
)
from tests.unit.nodes.node_overlay_setting_set.test_setting_set_goal_scope_ticket_rule import (
    rule_record,
)

pytestmark = pytest.mark.unit

# The settings the loader's tick lists (overlay_ids of node_overlay_store_load_effect) among the settable ones.
LOADER_ASSERTED = {"runtime_contract_hotload", "node_floor_fill_orchestrator"}


def test_only_the_overlays_the_loader_lists_keep_the_age_bound() -> None:
    declared = contract()["settable_settings"]
    kept = {row["overlay_id"] for row in declared if row.get("loader_asserted", True)}
    assert kept == LOADER_ASSERTED
    # One overlay id never mixes the two kinds.
    for overlay_id in {row["overlay_id"] for row in declared}:
        kinds = {
            row.get("loader_asserted", True)
            for row in declared
            if row["overlay_id"] == overlay_id
        }
        assert len(kinds) == 1, overlay_id


async def test_a_store_owned_overlay_idle_for_hours_is_still_set() -> None:
    old = rule_record() | {"loaded_at": (NOW - timedelta(hours=34)).isoformat()}
    handler, bus, _ = build({RULE_ID: old}, max_live_age_s=7200.0)
    result = await handler.handle(
        ModelOverlaySettingSetRequest.model_validate(
            {
                "overlay_id": RULE_ID,
                "key": "ticket_roots",
                "value": ["OMN-1", "OMN-9"],
                "actor": ACTOR,
            }
        )
    )
    assert result.new_value == ["OMN-1", "OMN-9"]
    assert len(bus.published) == 2


async def test_a_loader_asserted_overlay_still_refuses_an_old_record_positive_control() -> (
    None
):
    stale = live_record(loaded_at=NOW - timedelta(hours=34))
    handler, bus, _ = build({OVERLAY_ID: stale}, max_live_age_s=7200.0)
    with pytest.raises(LiveRecordStaleError):
        await handler.handle(
            ModelOverlaySettingSetRequest.model_validate(
                {
                    "overlay_id": OVERLAY_ID,
                    "key": KEY,
                    "value": BOOT_SET,
                    "actor": ACTOR,
                }
            )
        )
    assert bus.published == []
