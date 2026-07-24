# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""B12<->B6 seam regression: ModelReplayProjection/ModelReplayCursor/
ModelDeliveryPosition ``.model_fields`` must match the exported seam
fingerprint field-by-field (OMN-14779).

This is the omnimarket half of a two-repo cross-boundary regression test.
It imports ``omnibase_infra.contracts.canary.model_replay_projection_seam.json``
as *installed package data* (via ``importlib.resources``, requiring the
``omnibase-infra`` pin bumped to bbaa71864c6b96d8d3867debaef0fec137d3d97d in
this same PR, which packages the seam JSON as wheel data) and asserts the
real Pydantic ``model_fields`` on this side of the seam against it: name,
type, and nullability. No live DB, no ORM reflection.

The omnibase_infra half (``tests/unit/docker/test_canary_ddl_seam_fingerprint.py``
in that repo) drives the same seam JSON against the *parsed DDL text* of the
canary landing-table migration. If this test is green but that one is red (or
vice versa), the seam has drifted on one side only -- fix the seam JSON (if
the drift is intentional, requires a coordinated infra-side PR) or the
drifted model (if it is not), never widen this test to paper over a real
mismatch.
"""

from __future__ import annotations

import json
import types
from importlib.resources import files
from typing import Any, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_delivery_position import (
    ModelDeliveryPosition,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_replay_cursor import (
    ModelReplayCursor,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_replay_projection import (
    ModelReplayProjection,
)

JSONDict = dict[str, Any]

_MODELS_BY_NAME: dict[str, type[BaseModel]] = {
    "ModelReplayProjection": ModelReplayProjection,
    "ModelReplayCursor": ModelReplayCursor,
}

# ModelDeliveryPosition is not addressed by top-level DDL columns (it is the
# nested element type materialized inside the `cursor_positions` JSONB
# array) -- the seam JSON's `cursor_positions` narrowing_reason documents its
# shape in prose ("{topic, partition, offset} objects"). Mirrored here as an
# explicit structural expectation so a field rename/add/remove on
# ModelDeliveryPosition fails this test even though the seam JSON has no
# top-level entry to drive it directly.
_EXPECTED_DELIVERY_POSITION_FIELDS: dict[str, tuple[str, bool]] = {
    # field_name: (canonical_type_str, nullable)
    "topic": ("str", False),
    "partition": ("int", False),
    "offset": ("int", False),
}

# ModelReplayProjection.cursor / ModelReplayCursor.positions are container
# fields (nested models), not scalar DDL columns -- the seam maps their
# scalar *leaves* (cursor.token, cursor.positions, cursor.event_count)
# individually. Declared here so the reverse "every model field is seam
# accounted for" check does not spuriously fail on the containers themselves.
_CONTAINER_FIELDS_NOT_DIRECTLY_SEAM_MAPPED: dict[str, set[str]] = {
    "ModelReplayProjection": {"cursor"},
    "ModelReplayCursor": {"positions"},
}


def _load_seam() -> JSONDict:
    text = (
        files("omnibase_infra.contracts.canary")
        .joinpath("model_replay_projection_seam.json")
        .read_text(encoding="utf-8")
    )
    parsed: JSONDict = json.loads(text)
    return parsed


def _canonical_type_str(annotation: Any) -> str:
    """Render a runtime Pydantic field annotation in the seam JSON's
    ``source_type`` vocabulary (``"UUID | None"``, ``"tuple[X, ...]"``,
    ``"str"``, ``"int"``, ``"bool"``)."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        has_none = type(None) in args
        assert len(non_none) == 1, (
            f"seam fingerprint test only supports single-type Optional unions, got {annotation!r}"
        )
        rendered = _canonical_type_str(non_none[0])
        return f"{rendered} | None" if has_none else rendered
    if origin is tuple:
        args = get_args(annotation)
        err = f"seam fingerprint test only supports variadic tuple[T, ...] annotations, got {annotation!r}"
        assert len(args) == 2, err
        assert args[1] is Ellipsis, err
        return f"tuple[{_canonical_type_str(args[0])}, ...]"
    if hasattr(annotation, "__name__"):
        return str(annotation.__name__)
    return str(annotation)


def _is_nullable(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return type(None) in get_args(annotation)
    return False


def _resolve_model_and_field(source_field_path: str) -> tuple[type[BaseModel], str]:
    model_name, _, field_name = source_field_path.partition(".")
    assert model_name in _MODELS_BY_NAME, (
        f"seam entry references unknown model {model_name!r} "
        f"(source_field_path={source_field_path!r}); known models: "
        f"{sorted(_MODELS_BY_NAME)}"
    )
    return _MODELS_BY_NAME[model_name], field_name


@pytest.fixture(scope="module")
def seam() -> JSONDict:
    return _load_seam()


@pytest.mark.unit
class TestSeamArtifactShape:
    def test_seam_declares_the_canary_table(self, seam: JSONDict) -> None:
        assert seam["ddl_table"] == "delivery_replay_canary_projection"
        assert seam["ticket"] == "OMN-14779"

    def test_seam_has_at_least_one_field(self, seam: JSONDict) -> None:
        assert len(seam["fields"]) > 0


@pytest.mark.unit
class TestModelFieldsMatchSeamFieldByField:
    """Drives the real seam: seam-declared source fields vs the actual
    ModelReplayProjection / ModelReplayCursor ``model_fields``."""

    def test_every_seam_field_exists_on_the_model(self, seam: JSONDict) -> None:
        for entry in seam["fields"]:
            model, field_name = _resolve_model_and_field(entry["source_field_path"])
            assert field_name in model.model_fields, (
                f"seam declares {entry['source_field_path']!r} "
                f"(ddl_column={entry['ddl_column']!r}) but {model.__name__} "
                f"has no field {field_name!r} -- model has drifted from the seam."
            )

    def test_seam_type_matches_model_field_type(self, seam: JSONDict) -> None:
        for entry in seam["fields"]:
            model, field_name = _resolve_model_and_field(entry["source_field_path"])
            field_info = model.model_fields[field_name]
            actual = _canonical_type_str(field_info.annotation)
            assert actual == entry["source_type"], (
                f"{entry['source_field_path']}: model type {actual!r} != "
                f"seam-declared source_type {entry['source_type']!r}"
            )

    def test_seam_nullability_matches_model_field_nullability(
        self, seam: JSONDict
    ) -> None:
        for entry in seam["fields"]:
            model, field_name = _resolve_model_and_field(entry["source_field_path"])
            field_info = model.model_fields[field_name]
            actual = _is_nullable(field_info.annotation)
            assert actual == entry["source_nullable"], (
                f"{entry['source_field_path']}: model nullable={actual} != "
                f"seam-declared source_nullable={entry['source_nullable']}"
            )

    def test_no_undeclared_scalar_model_fields(self, seam: JSONDict) -> None:
        """Every scalar ModelReplayProjection/ModelReplayCursor field must be
        either seam-mapped or an explicitly declared container. Catches a
        field added to the model without an accompanying seam-JSON update
        (drift in the model -> DDL direction)."""
        declared_by_model: dict[str, set[str]] = {
            name: set() for name in _MODELS_BY_NAME
        }
        for entry in seam["fields"]:
            model, field_name = _resolve_model_and_field(entry["source_field_path"])
            declared_by_model[model.__name__].add(field_name)

        for model_name, model in _MODELS_BY_NAME.items():
            declared = declared_by_model[model_name]
            declared |= _CONTAINER_FIELDS_NOT_DIRECTLY_SEAM_MAPPED.get(
                model_name, set()
            )
            undeclared = set(model.model_fields) - declared
            assert not undeclared, (
                f"{model_name} fields {undeclared} are not represented in the "
                f"seam JSON (neither seam-mapped nor declared as a container) "
                f"-- update model_replay_projection_seam.json (coordinated "
                f"omnibase_infra PR) in the same change that adds this field."
            )


@pytest.mark.unit
class TestCorrelationIdNarrowingIsIntentional:
    """OMN-14779 acceptance #2: correlation_id is UUID|None on the pure B6
    model but NOT NULL PK on the landing table. Assert the model side of
    that deliberate narrowing is exactly what the seam JSON declares -- not
    a silent widen/narrow drift."""

    def test_correlation_id_seam_entry_flags_narrowing(self, seam: JSONDict) -> None:
        entries = [f for f in seam["fields"] if f["ddl_column"] == "correlation_id"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["source_field_path"] == "ModelReplayProjection.correlation_id"
        assert entry["source_nullable"] is True
        assert entry["narrowing"] is True
        assert entry["narrowing_reason"], "narrowing_reason must not be empty"

    def test_correlation_id_model_field_is_optional_uuid_default_none(self) -> None:
        field_info = ModelReplayProjection.model_fields["correlation_id"]
        assert _canonical_type_str(field_info.annotation) == "UUID | None"
        assert _is_nullable(field_info.annotation) is True
        assert field_info.default is None, (
            "correlation_id must default to None -- the compute node has no "
            "delivery context to require it; only the landing contract "
            "narrows it to NOT NULL PK."
        )


@pytest.mark.unit
class TestAcceptanceCoveredFields:
    """OMN-14779 acceptance #2 explicit field list: cursor fields, checksum,
    compared/diverged/divergence_reasons must all be present in the seam and
    resolvable on the real models."""

    @pytest.mark.parametrize(
        "source_field_path",
        [
            "ModelReplayCursor.token",
            "ModelReplayCursor.positions",
            "ModelReplayCursor.event_count",
            "ModelReplayProjection.projection_checksum",
            "ModelReplayProjection.compared",
            "ModelReplayProjection.diverged",
            "ModelReplayProjection.divergence_reasons",
        ],
    )
    def test_field_present_in_seam_and_on_model(
        self, seam: JSONDict, source_field_path: str
    ) -> None:
        paths = {f["source_field_path"] for f in seam["fields"]}
        assert source_field_path in paths
        model, field_name = _resolve_model_and_field(source_field_path)
        assert field_name in model.model_fields


@pytest.mark.unit
class TestDeliveryPositionShapeMatchesSeamNarrationOfCursorPositions:
    """ModelDeliveryPosition is the nested element type JSONB-materialized
    inside cursor_positions -- not a top-level seam field, but its shape is
    exactly what the seam's cursor_positions entry documents in prose
    ("{topic, partition, offset} objects"). Held here as an explicit
    structural assertion so a rename/add/remove on ModelDeliveryPosition
    fails CI even though no top-level seam entry drives it directly."""

    def test_cursor_positions_seam_entry_documents_position_shape(
        self, seam: JSONDict
    ) -> None:
        entries = [f for f in seam["fields"] if f["ddl_column"] == "cursor_positions"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["source_field_path"] == "ModelReplayCursor.positions"
        assert entry["source_type"] == "tuple[ModelDeliveryPosition, ...]"
        for token in ("topic", "partition", "offset"):
            assert token in entry["narrowing_reason"], (
                f"cursor_positions narrowing_reason no longer documents "
                f"{token!r} -- keep the seam JSON prose in sync with "
                f"ModelDeliveryPosition's real shape."
            )

    def test_delivery_position_fields_match_expected_shape(self) -> None:
        actual_fields = set(ModelDeliveryPosition.model_fields)
        assert actual_fields == set(_EXPECTED_DELIVERY_POSITION_FIELDS), (
            f"ModelDeliveryPosition fields {actual_fields} no longer match "
            f"the shape documented by the seam's cursor_positions entry "
            f"{set(_EXPECTED_DELIVERY_POSITION_FIELDS)} -- update both the "
            f"seam JSON's narrowing_reason and this test together."
        )
        for field_name, (
            expected_type,
            expected_nullable,
        ) in _EXPECTED_DELIVERY_POSITION_FIELDS.items():
            field_info = ModelDeliveryPosition.model_fields[field_name]
            assert _canonical_type_str(field_info.annotation) == expected_type, (
                f"ModelDeliveryPosition.{field_name} type drifted from "
                f"expected {expected_type!r}"
            )
            assert _is_nullable(field_info.annotation) is expected_nullable, (
                f"ModelDeliveryPosition.{field_name} nullability drifted "
                f"from expected {expected_nullable}"
            )
