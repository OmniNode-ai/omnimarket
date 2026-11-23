# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A set that is refused writes nothing and leaves no audit row: undeclared key, invalid value, no live record, stale record."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    LiveRecordStaleError,
    OverlayNotInStoreError,
    OverlaySettingSetRefusedError,
    SettingNotDeclaredError,
    SettingParentMissingError,
    SettingValueInvalidError,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from tests.unit.nodes.node_overlay_setting_set.support import (
    ACTOR,
    BOOT_SET,
    KEY,
    LIVE_LOADED_AT,
    NOW,
    OVERLAY_ID,
    build,
    hotload_document,
    live_record,
)

pytestmark = pytest.mark.unit


def request(**fields: object) -> ModelOverlaySettingSetRequest:
    base: dict[str, object] = {
        "overlay_id": OVERLAY_ID,
        "key": KEY,
        "value": BOOT_SET,
        "actor": ACTOR,
    }
    return ModelOverlaySettingSetRequest.model_validate({**base, **fields})


async def test_undeclared_key_is_refused_with_a_typed_error() -> None:
    handler, bus, _ = build()
    with pytest.raises(SettingNotDeclaredError) as raised:
        await handler.handle(request(key="lanes.lane-a.enabled", value=False))
    # The error names what is declared for that overlay, so the operator can correct the key.
    assert "lanes.{lane}.lazy_start" in str(raised.value)
    assert isinstance(raised.value, OverlaySettingSetRefusedError)
    assert bus.published == []


async def test_a_key_declared_for_another_overlay_is_refused_for_this_one() -> None:
    handler, bus, _ = build()
    with pytest.raises(SettingNotDeclaredError):
        await handler.handle(request(overlay_id="lab_hosts"))
    assert bus.published == []


async def test_the_declared_key_is_accepted_positive_control() -> None:
    handler, bus, _ = build()
    await handler.handle(request())
    assert len(bus.published) == 2


@pytest.mark.parametrize(
    ("case", "error", "fields"),
    [
        ("undeclared_key", SettingNotDeclaredError, {"key": "lanes.lane-a.mode"}),
        (
            "key_below_the_declared_one",
            SettingNotDeclaredError,
            {"key": "lanes.lane-a.lazy_start.budget_seconds", "value": 5},
        ),
        ("key_missing_the_lane", SettingNotDeclaredError, {"key": "lanes.lazy_start"}),
        ("blank_lane_segment", SettingNotDeclaredError, {"key": "lanes..lazy_start"}),
        (
            "invalid_value_budget_zero",
            SettingValueInvalidError,
            {"value": {**BOOT_SET, "budget_seconds": 0}},
        ),
        (
            "invalid_value_unknown_field",
            SettingValueInvalidError,
            {"value": {**BOOT_SET, "surprise": 1}},
        ),
        (
            "invalid_value_repeated_boot_contract",
            SettingValueInvalidError,
            {"value": {**BOOT_SET, "boot_contracts": ["a", "a"]}},
        ),
    ],
)
async def test_no_audit_row_and_no_record_on_refusal(
    case: str, error: type[Exception], fields: dict[str, object]
) -> None:
    handler, bus, _ = build()
    with pytest.raises(error):
        await handler.handle(request(**fields))
    assert bus.published == [], case


async def test_no_audit_row_when_the_overlay_is_not_in_the_store() -> None:
    handler, bus, _ = build(live={})
    with pytest.raises(OverlayNotInStoreError):
        await handler.handle(request())
    assert bus.published == []


async def test_no_audit_row_when_the_lane_row_is_missing() -> None:
    document = hotload_document()
    lanes: Any = document["lanes"]
    del lanes["lane-a"]
    handler, bus, _ = build(live={OVERLAY_ID: live_record(document)})
    with pytest.raises(SettingParentMissingError):
        await handler.handle(request())
    assert bus.published == []


async def test_a_stale_live_record_is_refused_so_an_old_read_never_replaces_newer_settings() -> (
    None
):
    stale = live_record(loaded_at=NOW - timedelta(hours=3))
    handler, bus, _ = build(live={OVERLAY_ID: stale}, max_live_age_s=7200.0)
    with pytest.raises(LiveRecordStaleError):
        await handler.handle(request())
    assert bus.published == []
    # The same record one minute inside the bound is accepted: the bound is what refused it.
    fresh = live_record(loaded_at=NOW - timedelta(seconds=7140))
    handler, bus, _ = build(live={OVERLAY_ID: fresh}, max_live_age_s=7200.0)
    await handler.handle(request())
    assert len(bus.published) == 2
    assert LIVE_LOADED_AT < NOW


@pytest.mark.parametrize("actor", ["", "   "])
def test_a_set_with_no_actor_is_not_a_request(actor: str) -> None:
    with pytest.raises(ValidationError):
        request(actor=actor)
