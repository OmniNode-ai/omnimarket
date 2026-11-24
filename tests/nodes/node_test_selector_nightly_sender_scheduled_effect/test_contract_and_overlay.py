# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: the contract binds the tick, the schedule overlay and the terminal event, and an overlay missing a key is refused by name."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.handlers.adapter_bus_command_sender import (
    load_command_topics,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender_schedule_overlay import (
    ModelNightlySenderScheduleOverlay,
)
from tests.nodes.node_test_selector_nightly_sender_scheduled_effect.support import (
    NODE,
    OVERLAY,
    REPO,
    write_overlay,
)

pytestmark = pytest.mark.unit

NODE_DIR = REPO / "src/omnimarket/nodes" / NODE
COLLECT_COMMAND = "onex.cmd.omnimarket.test-selector-collect-nightly-requested.v1"
PUBLISH_COMMAND = "onex.cmd.omnimarket.test-selector-publish-record-requested.v1"
COLLECT_TERMINAL = "onex.evt.omnimarket.test-selector-collect-completed.v1"
PUBLISH_TERMINAL = "onex.evt.omnimarket.test-selector-record-published.v1"
TICK = "onex.intent.platform.runtime-tick.v1"
RUN_EVENT = "onex.evt.omnimarket.schedule-run.v1"


def contract() -> dict[str, Any]:
    loaded = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def load_with(
    tmp_path: Path, document: dict[str, object]
) -> ModelNightlySenderScheduleOverlay:
    write_overlay(tmp_path, document)
    return load_structured_overlay(
        NODE,
        ModelNightlySenderScheduleOverlay,
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
    )


def test_contract_and_overlay_the_contract_binds_the_tick_the_overlay_and_the_terminal_event() -> (
    None
):
    loaded = contract()
    assert loaded["input_subscriptions"] == [
        {"topic": TICK, "operation": "nightly_sender.scheduled_tick"}
    ]
    assert loaded["event_bus"]["subscribe_topics"] == [TICK]
    assert loaded["terminal_event"] == RUN_EVENT
    assert RUN_EVENT in loaded["event_bus"]["publish_topics"]
    declared = loaded["runtime"]["schedule_overlay"]
    assert declared["overlay_id"] == NODE
    module, _, name = str(declared["model"]).rpartition(".")
    assert (
        getattr(importlib.import_module(module), name)
        is ModelNightlySenderScheduleOverlay
    )
    routed = loaded["handler_routing"]["handlers"][0]
    assert routed["operation"] == "nightly_sender.scheduled_tick"
    command_timeout = OVERLAY["command_timeout_seconds"]
    assert isinstance(command_timeout, int)
    assert loaded["descriptor"]["timeout_ms"] >= 2 * 1000 * command_timeout


def test_contract_and_overlay_the_two_commands_and_their_terminal_events_are_declared() -> (
    None
):
    loaded = contract()
    assert load_command_topics("collect_nightly").command == COLLECT_COMMAND
    assert load_command_topics("collect_nightly").success == COLLECT_TERMINAL
    assert load_command_topics("publish_attribution_record").command == PUBLISH_COMMAND
    assert load_command_topics("publish_attribution_record").success == PUBLISH_TERMINAL
    publish = loaded["event_bus"]["publish_topics"]
    assert {COLLECT_COMMAND, PUBLISH_COMMAND} <= set(publish)
    grants = {
        (g["name"], g["operation"]) for g in loaded["broker_acl_grants"][0]["grants"]
    }
    for topic in (COLLECT_COMMAND, PUBLISH_COMMAND, RUN_EVENT):
        assert (topic, "WRITE") in grants
        assert (topic, "DESCRIBE") in grants
    # The role derivation covers event_bus topics only, so the terminal events
    # carry no grant block rows; the tick is the one subscribed topic.
    assert (TICK, "READ") in grants
    assert (TICK, "DESCRIBE") in grants


def test_contract_and_overlay_the_example_overlay_holds_exactly_the_models_keys() -> (
    None
):
    example = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert set(example) == set(ModelNightlySenderScheduleOverlay.model_fields)
    parsed = ModelNightlySenderScheduleOverlay.model_validate(example)
    assert parsed.nightly_slot_time_utc.isoformat(timespec="minutes") == "02:30"
    # the one optional key, the mounted config root, is exercised in test_config_root_bus_open
    assert set(OVERLAY) == set(ModelNightlySenderScheduleOverlay.model_fields) - {
        "workspace_config_root"
    }


@pytest.mark.parametrize("key", sorted(OVERLAY))
def test_contract_and_overlay_an_overlay_missing_a_key_is_refused_naming_the_key(
    tmp_path: Path, key: str
) -> None:
    document = {k: v for k, v in OVERLAY.items() if k != key}
    with pytest.raises(NodeOverlayError) as refused:
        load_with(tmp_path, document)
    assert key in str(refused.value)


def test_contract_and_overlay_a_complete_overlay_is_accepted_and_an_extra_key_is_refused(
    tmp_path: Path,
) -> None:
    assert load_with(tmp_path, dict(OVERLAY)).collector_max_lag_hours == 6
    with pytest.raises(NodeOverlayError) as refused:
        load_with(tmp_path, {**OVERLAY, "unexpected_key": 1})
    assert "unexpected_key" in str(refused.value)


def test_contract_and_overlay_a_check_time_before_the_slot_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(NodeOverlayError):
        load_with(tmp_path, {**OVERLAY, "earliest_check_time_utc": "02:00"})


def test_contract_and_overlay_the_node_adds_no_script_timer_or_workflow() -> None:
    suffixes = {
        p.suffix
        for p in NODE_DIR.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    assert suffixes <= {".py", ".yaml"}
    # the schedule is the runtime tick and the overlay; the contract declares no timer, cron or poller block of its own
    assert not {"schedule", "cron", "timer"} & set(contract())
