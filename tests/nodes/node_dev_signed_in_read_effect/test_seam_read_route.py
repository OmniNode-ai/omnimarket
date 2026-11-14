# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of the dev signed-in read node (OMN-24345): what the overlay names must be what the other side declares.

1. The read's route, query names, data-state field and data states are the by-correlation read node's own: its contract's
   http_route path, its request model's field names and its response model's data_state values.
2. The signed-in user, its sign-in client and where its password is stored are the dev identity node's overlay values.
Each is a document the other node owns, so a rename on either side alone fails here, not on the dev runtime. The failing
controls each rename one field.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_hook_events_by_correlation_read_effect.models import (
    ModelHookEventsByCorrelation,
    ModelHookEventsReadRequest,
)

from ._support import overlay_document

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
_READ_CONTRACT = _NODES / "node_hook_events_by_correlation_read_effect/contract.yaml"
_IDENTITY_EXAMPLE = _NODES / "node_dev_identity_provision_effect/overlay.example.yaml"


def route_problems(document: dict[str, Any]) -> list[str]:
    contract = yaml.safe_load(_READ_CONTRACT.read_text(encoding="utf-8"))
    read = document["read"]
    found: list[str] = []
    if read["path"] != contract["http_route"]["path"]:
        found.append("path differs from the by-correlation contract's http_route path")
    request_fields = set(ModelHookEventsReadRequest.model_fields)
    for label in ("correlation_param", "limit_param"):
        if read[label] not in request_fields:
            found.append(f"{label} {read[label]!r} is not a field of the read request")
    if read["data_state_field"] not in ModelHookEventsByCorrelation.model_fields:
        found.append("data_state_field is not a field of the read response")
    declared = set(
        typing.get_args(
            ModelHookEventsByCorrelation.model_fields["data_state"].annotation
        )
    )
    if set(read["data_states"]) != declared:
        found.append(
            f"data_states {sorted(read['data_states'])} differ from {sorted(declared)}"
        )
    if read["limit"] > contract["http_route"]["max_limit"]:
        found.append("limit exceeds the read's max_limit")
    if (
        len(read["correlation_id"])
        > contract["http_route"]["max_correlation_id_length"]
    ):
        found.append("correlation_id is longer than the read accepts")
    return found


def identity_problems(document: dict[str, Any]) -> list[str]:
    identity = yaml.safe_load(_IDENTITY_EXAMPLE.read_text(encoding="utf-8"))["user"]
    ours = document["user"]
    return [
        f"user.{key} {ours[key]!r} differs from the identity node's {identity[key]!r}"
        for key in ("username", "client_id", "password_folder", "password_key")
        if ours[key] != identity[key]
    ]


def test_route_matches_the_read_node() -> None:
    assert route_problems(overlay_document()) == []


def test_user_matches_the_identity_node() -> None:
    assert identity_problems(overlay_document()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "/v1/projections/hook-events/other"),
        ("correlation_param", "correlation"),
        ("limit_param", "max_rows"),
        ("data_state_field", "state"),
        ("data_states", ["found", "not_found"]),
        ("limit", 101),
        ("correlation_id", "x" * 65),
    ],
)
def test_a_renamed_route_fact_is_reported(field: str, value: Any) -> None:
    document = overlay_document()
    document["read"][field] = value
    assert len(route_problems(document)) == 1


@pytest.mark.parametrize(
    "key", ["username", "client_id", "password_folder", "password_key"]
)
def test_a_renamed_user_fact_is_reported(key: str) -> None:
    document = overlay_document()
    document["user"][key] = "renamed"
    assert len(identity_problems(document)) == 1
