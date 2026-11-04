# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Layer identities and lifetimes are validated against supplied overlay bounds."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.model_host_layer import ModelHostLayerRequest, ModelLayerBounds

pytestmark = pytest.mark.unit


def _request_data() -> dict[str, object]:
    return {
        "request_id": "request-a",
        "layer_id": "layer-a",
        "expected_revision": "generation-1",
        "entries": [
            {
                "host": "host-a",
                "surface": "runtime",
                "repository": "example-repo",
                "ref": "v1.2.3",
                "commit": "a" * 40,
            }
        ],
        "requested_by": "operator",
        "requested_at": "2026-01-02T03:04:05Z",
    }


@pytest.mark.parametrize("field", ["expected_revision", "request_id"])
@pytest.mark.parametrize("missing", [True, False])
def test_layer_request_requires_revision_and_id(field: str, *, missing: bool) -> None:
    data = _request_data()
    if missing:
        data.pop(field)
    else:
        data[field] = ""
    with pytest.raises(ValidationError, match=field):
        ModelHostLayerRequest.model_validate(data)
    assert (
        ModelHostLayerRequest.model_validate(_request_data()).request_id == "request-a"
    )


@pytest.mark.parametrize("field", ["default_life_hours", "max_life_days"])
@pytest.mark.parametrize("value", [None, 0, -1])
def test_layer_life_bounds_from_overlay_requires_positive_bounds(
    field: str, value: int | None
) -> None:
    data: dict[str, object] = {"default_life_hours": 12, "max_life_days": 2}
    if value is None:
        data.pop(field)
    else:
        data[field] = value
    with pytest.raises(ValidationError, match=field):
        ModelLayerBounds.model_validate(data)


def test_layer_life_bounds_from_overlay() -> None:
    short = ModelLayerBounds(default_life_hours=12, max_life_days=2)
    long = ModelLayerBounds(default_life_hours=36, max_life_days=4)
    request = ModelHostLayerRequest.model_validate(_request_data())
    assert request.resolved_life_hours(short) == 12
    assert request.resolved_life_hours(long) == 36
    data = {**_request_data(), "life_hours": 60}
    explicit = ModelHostLayerRequest.model_validate(data)
    assert explicit.resolved_life_hours(long) == 60
    with pytest.raises(ValueError, match="max"):
        explicit.resolved_life_hours(short)
    boundary = ModelHostLayerRequest.model_validate(
        {**_request_data(), "life_hours": short.max_life_days * 24}
    )
    assert boundary.resolved_life_hours(short) == short.max_life_days * 24
    with pytest.raises(ValidationError, match="default_life_hours"):
        ModelLayerBounds(default_life_hours=25, max_life_days=1)


def test_layer_life_hardcoded_default() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src/omnimarket/models/model_host_layer.py"
    ).read_text(encoding="utf-8")
    pattern = r"\b(48|7)\b"
    assert re.search(pattern, source) is None
    assert re.search(pattern, "default_life_hours = 48") is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("layer_id", ""),
        ("entries", []),
        ("requested_by", ""),
        ("life_hours", 0),
        ("life_hours", -1),
    ],
)
def test_layer_request_field_constraints(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        ModelHostLayerRequest.model_validate({**_request_data(), field: value})


def test_layer_bounds_frozen_and_extra_forbidden() -> None:
    bounds = ModelLayerBounds(default_life_hours=12, max_life_days=2)
    field = "max_life_days"
    with pytest.raises(ValidationError, match="frozen"):
        setattr(bounds, field, 3)
    with pytest.raises(ValidationError, match="extra"):
        ModelLayerBounds.model_validate({**bounds.model_dump(), "extra": True})
