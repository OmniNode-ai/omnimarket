# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-state panel models: aware-only datetimes, a stable digest, the row invariants and the overlay's uniqueness."""

from __future__ import annotations

import datetime
import hashlib
import json
from datetime import timedelta, timezone

import pytest
from pydantic import ValidationError

from omnimarket.models.work_state_panel import (
    EnumPanelRowState,
    ModelWorkStatePanelBody,
    ModelWorkStatePanelEntry,
    ModelWorkStatePanelOverlay,
    ModelWorkStatePanelRow,
)
from omnimarket.models.work_state_projection.enum_work_item_state import (
    EnumWorkItemState,
)
from omnimarket.models.work_state_projection.enum_work_state_source import (
    EnumWorkStateSource,
)
from omnimarket.models.work_state_projection.model_work_state_projected_event import (
    EnumWorkStateProjectorProfile,
)


def _body(**overrides: object) -> ModelWorkStatePanelBody:
    values: dict[str, object] = {
        "as_of": datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
        "computed_at": datetime.datetime(2026, 1, 2, 3, 4, 6, tzinfo=datetime.UTC),
        "projector": EnumWorkStateProjectorProfile.UNREAD,
        "item_count": 1,
        "counts": {EnumWorkItemState.AVAILABLE: 1},
        "ready_in_scope": 1,
        "unread": (EnumWorkStateSource.LINEAR,),
        "stale_sources": (),
    }
    values.update(overrides)
    return ModelWorkStatePanelBody.model_validate(values)


def _entry(**overrides: object) -> ModelWorkStatePanelEntry:
    values: dict[str, object] = {
        "panel_id": "panel-1",
        "projector": EnumWorkStateProjectorProfile.UNREAD,
        "topic": "work-state.projection",
        "max_age_s": 60,
        "max_future_skew_s": 5,
        "accept_deadline_s": 30,
        "audience": "team_service",
        "allowed_readers": ("reader-1",),
        "answer_writer": "writer-1",
        "reducer_group": "group-1",
        "start_position": "latest",
    }
    values.update(overrides)
    return ModelWorkStatePanelEntry.model_validate(values)


@pytest.mark.unit
def test_naive_datetime_in_the_body_raises() -> None:
    with pytest.raises(ValidationError):
        _body(as_of=datetime.datetime(2026, 1, 2, 3, 4, 5))
    with pytest.raises(ValidationError):
        _body(computed_at=datetime.datetime(2026, 1, 2, 3, 4, 6))


@pytest.mark.unit
def test_aware_datetime_is_converted_to_utc() -> None:
    body = _body(
        as_of=datetime.datetime(
            2026, 1, 2, 5, 4, 5, tzinfo=timezone(timedelta(hours=2))
        ),
    )
    assert body.as_of == datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
    assert body.as_of.tzinfo.utcoffset(body.as_of) == timedelta(0)


@pytest.mark.unit
def test_digest_is_equal_for_the_same_instant_given_at_two_offsets() -> None:
    utc = _body(as_of=datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC))
    offset = _body(
        as_of=datetime.datetime(
            2026, 1, 2, 5, 4, 5, tzinfo=timezone(timedelta(hours=2))
        ),
    )
    assert utc.digest() == offset.digest()
    assert (
        utc.digest()
        == hashlib.sha256(
            json.dumps(
                utc.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )


@pytest.mark.unit
def test_counts_key_outside_the_enum_raises() -> None:
    with pytest.raises(ValidationError):
        _body(counts={"bogus": 1})
    with pytest.raises(ValidationError):
        _body(counts={EnumWorkItemState.AVAILABLE: -1})


@pytest.mark.unit
def test_unread_entry_outside_the_enum_raises() -> None:
    with pytest.raises(ValidationError):
        _body(unread=("bogus",))


@pytest.mark.unit
def test_last_refusal_id_pattern_is_enforced() -> None:
    accepted = ModelWorkStatePanelRow(last_refusal_id="refused-" + "0" * 64)
    assert accepted.last_refusal_id == "refused-" + "0" * 64
    with pytest.raises(ValidationError):
        ModelWorkStatePanelRow(last_refusal_id="refused-" + "z" * 64)
    with pytest.raises(ValidationError):
        ModelWorkStatePanelRow(last_refusal_id="refused-0" * 1)
    assert ModelWorkStatePanelRow().last_refusal_id is None


@pytest.mark.unit
def test_row_state_is_empty_or_held() -> None:
    assert ModelWorkStatePanelRow().state is EnumPanelRowState.EMPTY
    assert ModelWorkStatePanelRow(publication_id="pub-1").state is (
        EnumPanelRowState.HELD
    )


@pytest.mark.unit
def test_overlay_rejects_duplicate_panel_ids_and_bindings() -> None:
    with pytest.raises(ValidationError):
        ModelWorkStatePanelOverlay(panels=(_entry(), _entry()))
    with pytest.raises(ValidationError):
        ModelWorkStatePanelOverlay(
            panels=(
                _entry(),
                _entry(panel_id="panel-2", topic="work-state.projection"),
            ),
        )
    overlay = ModelWorkStatePanelOverlay(
        panels=(_entry(), _entry(panel_id="panel-2", topic="other.topic")),
    )
    assert overlay.entry_for("panel-1").panel_id == "panel-1"
    assert overlay.entry_for("panel-2").panel_id == "panel-2"
    assert overlay.entry_for("missing") is None


@pytest.mark.unit
def test_entry_without_max_age_s_raises() -> None:
    values: dict[str, object] = {
        "panel_id": "panel-1",
        "projector": EnumWorkStateProjectorProfile.UNREAD,
        "topic": "work-state.projection",
        "max_future_skew_s": 5,
        "accept_deadline_s": 30,
        "audience": "team_service",
        "allowed_readers": ("reader-1",),
        "answer_writer": "writer-1",
        "reducer_group": "group-1",
        "start_position": "latest",
    }
    with pytest.raises(ValidationError):
        ModelWorkStatePanelEntry.model_validate(values)


def test_overlay_row_reader_route_defaults_to_none_and_validates() -> None:
    from pydantic import ValidationError

    from omnimarket.models.work_state_panel import ModelWorkStatePanelRowReaderRoute

    ok = ModelWorkStatePanelRowReaderRoute(
        dsn_env="SOME_DSN_ENV", table="work_state_panel_state", timeout_s=5
    )
    assert ok.table == "work_state_panel_state"
    for bad in (
        {"dsn_env": "", "table": "t", "timeout_s": 5},
        {"dsn_env": "E", "table": "bad-table; drop", "timeout_s": 5},
        {"dsn_env": "E", "table": "t", "timeout_s": 0},
    ):
        try:
            ModelWorkStatePanelRowReaderRoute(**bad)
        except ValidationError:
            continue
        raise AssertionError(f"accepted {bad}")
