# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Desired and observed host state validation and canonical wire contracts."""

import json

import pytest
from pydantic import ValidationError

from omnimarket.models.model_host_desired_state import (
    ModelHostDesiredEntry,
    ModelHostDesiredState,
)
from omnimarket.models.model_host_layer import (
    ModelHostLayerDecision,
    ModelHostLayerRequest,
    ModelHostLayerTombstone,
)
from omnimarket.models.model_host_observed_state import ModelHostObservedState
from omnimarket.models.model_host_wire import ModelHostWire

pytestmark = pytest.mark.unit
STAMP = "2026-01-02T03:04:05Z"


def _entry() -> ModelHostDesiredEntry:
    return ModelHostDesiredEntry(
        host="host-a",
        surface="runtime",
        repository="example-repo",
        ref="v1.2.3",
        commit="a" * 40,
    )


def _records() -> tuple[ModelHostWire, ...]:
    return (
        ModelHostDesiredState(
            generation=1,
            sequence=0,
            published_at=STAMP,
            publisher="publisher",
            hosts=("host-a",),
            entries=(_entry(),),
        ),
        ModelHostObservedState(
            host="host-a",
            surface="runtime",
            generation=0,
            sequence=0,
            observed_at=STAMP,
            status="current",
            installed_revision=None,
            loaded_revision="a" * 40,
            detail="révision",
        ),
        ModelHostLayerRequest(
            request_id="request-a",
            layer_id="layer-a",
            expected_revision="generation-1",
            entries=(_entry(),),
            requested_by="operator",
            requested_at=STAMP,
            life_hours=None,
        ),
        ModelHostLayerDecision(
            layer_id="layer-a",
            request_id="request-a",
            decision="accepted",
            reason="approved",
            decided_at=STAMP,
            expires_at=STAMP,
            generation=1,
        ),
        ModelHostLayerTombstone(layer_id="layer-a", removed_at=STAMP, reason="expired"),
    )


@pytest.mark.parametrize(
    "ref",
    ["v1.2.3", "refs/tags/v1.2.3", "1.2.3", "v1.2.3-rc.1+build", "refs/tags/release"],
)
def test_ref_must_be_tag_accepts_tags(ref: str) -> None:
    data = _entry().model_dump()
    data["ref"] = ref
    assert ModelHostDesiredEntry.model_validate(data).ref == ref


@pytest.mark.parametrize(
    "ref",
    [
        "a" * 40,
        "abcdef0",
        "A" * 64,
        "main",
        "feature/topic",
        "refs/heads/main",
        "refs/tags/",
        "refs/tags/release..candidate",
        "v1.2.3..build",
        "v1.2.3\n",
        "refs/tags/invalid tag",
    ],
)
def test_ref_must_be_tag_refuses_other_references(ref: str) -> None:
    data = _entry().model_dump()
    data["ref"] = ref
    with pytest.raises(ValidationError, match="ref"):
        ModelHostDesiredEntry.model_validate(data)


@pytest.mark.parametrize("record", _records(), ids=lambda record: type(record).__name__)
def test_host_state_roundtrip(record: ModelHostWire) -> None:
    canonical = record.to_wire()
    expected = json.dumps(
        record.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert canonical == expected
    assert type(record).from_wire(canonical).to_wire() == canonical
    reordered = dict(reversed(tuple(record.model_dump(mode="json").items())))
    alternate = json.dumps(reordered, ensure_ascii=False, indent=2).encode("utf-8")
    assert alternate != canonical
    assert type(record).from_wire(alternate).to_wire() == canonical


def test_host_state_keys() -> None:
    desired, observed, request, decision, tombstone = _records()
    assert isinstance(desired, ModelHostDesiredState)
    assert isinstance(observed, ModelHostObservedState)
    assert isinstance(request, ModelHostLayerRequest)
    assert isinstance(decision, ModelHostLayerDecision)
    assert isinstance(tombstone, ModelHostLayerTombstone)
    assert desired.key == "fleet"
    assert desired.fold_key == (1, 0)
    assert observed.key == "host-a/runtime"
    assert observed.fold_key == (0, 0)
    assert request.key == decision.key == tombstone.key == "layer-a"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", ""),
        ("surface", ""),
        ("surface", "Runtime"),
        ("surface", "-runtime"),
        ("repository", ""),
        ("repository", "owner/repository"),
        ("repository", "https://example.com/repository"),
        ("repository", ".repository"),
        ("commit", "A" * 40),
        ("commit", "a" * 39),
        ("commit", "g" * 40),
    ],
)
def test_host_state_entry_constraints(field: str, value: str) -> None:
    data = _entry().model_dump()
    data[field] = value
    with pytest.raises(ValidationError, match=field):
        ModelHostDesiredEntry.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation", 0),
        ("sequence", -1),
        ("publisher", ""),
        ("hosts", [""]),
        ("hosts", ["host-a", "host-a"]),
        ("hosts", ["host-b"]),
        ("entries", [_entry().model_dump(), _entry().model_dump()]),
    ],
)
def test_host_state_desired_constraints(field: str, value: object) -> None:
    data = _records()[0].model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        ModelHostDesiredState.model_validate(data)


@pytest.mark.parametrize("record", _records(), ids=lambda record: type(record).__name__)
@pytest.mark.parametrize(
    "stamp",
    ["2026-02-30T03:04:05Z", "2026-01-02T03:04:05+00:00", "2026-01-02T25:00:00Z"],
)
def test_host_state_timestamp_validation(record: ModelHostWire, stamp: str) -> None:
    data = record.model_dump()
    field = next(name for name in data if name.endswith("_at"))
    data[field] = stamp
    with pytest.raises(ValidationError, match=field):
        type(record).model_validate(data)


@pytest.mark.parametrize("record", [*_records(), _entry()])
def test_host_state_frozen_and_extra_forbidden(record: ModelHostWire) -> None:
    field = next(iter(type(record).model_fields))
    with pytest.raises(ValidationError, match="frozen"):
        setattr(record, field, getattr(record, field))
    with pytest.raises(ValidationError, match="extra"):
        type(record).model_validate({**record.model_dump(), "extra": True})


@pytest.mark.parametrize(
    "status", ["current", "lagging", "failed", "refused", "unreadable", "absent"]
)
def test_host_state_observed_statuses(status: str) -> None:
    data = _records()[1].model_dump()
    data["status"] = status
    data.pop("detail")
    assert ModelHostObservedState.model_validate(data).detail == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", ""),
        ("surface", "Bad"),
        ("generation", -1),
        ("sequence", -1),
        ("status", "unknown"),
    ],
)
def test_host_state_observed_constraints(field: str, value: object) -> None:
    data = _records()[1].model_dump()
    data[field] = value
    with pytest.raises(ValidationError, match=field):
        ModelHostObservedState.model_validate(data)
