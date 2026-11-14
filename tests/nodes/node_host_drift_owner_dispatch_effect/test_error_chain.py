# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain of the host drift owner dispatch: a refused placement is a typed lane entry, retried by the next pass (OMN-24239)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.adapters.runtime_adapters import (
    FileDriftOpenItems,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelDriftOpenItem,
    ModelDriftOwnerRequest,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.overlay import (
    ModelDriftOwnerOverlay,
    load_overlay,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.protocols import (
    DriftStateUnreadableError,
)

from .support import NODE, item, rig

pytestmark = pytest.mark.unit

CAUSE = "host-drift:h-example:env-file:host-env"


def test_a_refused_placement_is_a_failed_lane_that_is_not_recorded_and_is_retried() -> (
    None
):
    r = rig((item(),))
    r.runner.refuse = "no placement: every host is over its bar"
    first = r.handler.handle(ModelDriftOwnerRequest(dry_run=False))
    (lane,) = first.lanes
    assert not lane.started
    assert not lane.dry_run
    assert lane.detail == "no placement: every host is over its bar"
    assert r.record.record == {}
    assert r.runner.placed == []

    r.runner.refuse = None
    retry = r.handler.handle(ModelDriftOwnerRequest(dry_run=False))
    assert [x.started for x in retry.lanes] == [True]
    assert set(r.record.record) == {CAUSE}


def test_an_excluded_kind_and_an_excluded_key_render_no_row() -> None:
    r = rig(
        (
            item("heartbeat:h201", kind="heartbeat"),
            item("unit:example-drift", kind="unit"),
        )
    )
    result = r.handler.handle(ModelDriftOwnerRequest(dry_run=False))
    assert result.items_read == 2
    assert result.rows_rendered == 0
    assert result.lanes == ()


def test_a_malformed_first_seen_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError, match="first_seen_at"):
        item(first_seen_at="2026-02-30T00:00:00Z")


def test_an_overlay_with_a_missing_key_is_refused_and_names_the_key() -> None:
    document = yaml.safe_load(
        (NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    del document["dispatch"]["model"]
    with pytest.raises(ValidationError, match="model"):
        ModelDriftOwnerOverlay.model_validate(document)


def test_an_overlay_with_an_unknown_key_is_refused() -> None:
    document = yaml.safe_load(
        (NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    document["dispatch"]["default_model"] = "sonnet"
    with pytest.raises(ValidationError, match="default_model"):
        ModelDriftOwnerOverlay.model_validate(document)


def test_no_overlay_root_refuses_the_load() -> None:
    with pytest.raises(Exception, match="node_host_drift_owner_dispatch_effect"):
        load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": ""})


def test_a_missing_state_file_is_unreadable_and_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(DriftStateUnreadableError, match=r"absent\.json"):
        FileDriftOpenItems(tmp_path / "absent.json").read()


def test_a_malformed_state_file_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"host": "h", "open": [{"key": "k"}]}', encoding="utf-8")
    with pytest.raises(DriftStateUnreadableError, match=r"state\.json"):
        FileDriftOpenItems(path).read()


def test_the_item_model_refuses_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ModelDriftOpenItem.model_validate({**item().model_dump(), "extra": 1})
