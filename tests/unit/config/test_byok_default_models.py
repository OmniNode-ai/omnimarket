# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21215: the catalogue recommends models per row and per Mac memory class.

Every provider row carries an ordered ``default_models`` list (cheapest first),
the ``local`` block recommends one model per memory class (16 GB, 32 GB, 64 GB
and up) with the server that runs it, and exactly one row is ``first_key``: the
key a new developer adds first.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from omnimarket.routing.byok_provider_backends import (
    CATALOG_PATH,
    ByokCatalogError,
    _read_plan_catalog,
    first_key_backend,
    load_byok_local_recommendation,
    load_byok_plan_catalog,
    recommended_local_model,
)

pytestmark = pytest.mark.unit


def test_every_catalogue_row_declares_a_non_empty_default_models_list() -> None:
    for row in load_byok_plan_catalog().values():
        assert isinstance(row.default_models, tuple)
        assert row.default_models
        assert all(
            isinstance(model_id, str) and model_id for model_id in row.default_models
        )
        assert len(set(row.default_models)) == len(row.default_models)


def test_every_default_model_matches_its_rows_model_preference() -> None:
    for row in load_byok_plan_catalog().values():
        for model_id in row.default_models:
            assert any(
                re.fullmatch(pref.pattern, model_id) for pref in row.model_preference
            )


def test_the_local_row_has_one_entry_per_memory_class_with_a_server() -> None:
    local = load_byok_local_recommendation()

    assert isinstance(local.memory_classes, tuple)
    assert len(local.memory_classes) == 3
    by_class = {entry.memory_class: entry for entry in local.memory_classes}

    assert set(by_class) == {"16gb", "32gb", "64gb_plus"}
    assert by_class["16gb"].min_memory_gb == 16
    assert by_class["32gb"].min_memory_gb == 32
    assert by_class["64gb_plus"].min_memory_gb == 64

    for entry in local.memory_classes:
        assert entry.model
        assert entry.server

    assert isinstance(local.default_models, tuple)
    assert local.default_models
    expected_models = tuple(
        by_class[name].model for name in ("16gb", "32gb", "64gb_plus")
    )
    assert local.default_models == expected_models


def test_recommended_local_model_picks_by_memory() -> None:
    local = load_byok_local_recommendation()
    by_class = {entry.memory_class: entry for entry in local.memory_classes}

    assert recommended_local_model(8) is None

    result = recommended_local_model(16)
    assert result is not None
    assert result.memory_class == "16gb"
    assert result == by_class["16gb"]

    result = recommended_local_model(31)
    assert result is not None
    assert result.memory_class == "16gb"
    assert result == by_class["16gb"]

    result = recommended_local_model(32)
    assert result is not None
    assert result.memory_class == "32gb"
    assert result == by_class["32gb"]

    result = recommended_local_model(64)
    assert result is not None
    assert result.memory_class == "64gb_plus"
    assert result == by_class["64gb_plus"]

    result = recommended_local_model(128)
    assert result is not None
    assert result.memory_class == "64gb_plus"
    assert result == by_class["64gb_plus"]


def test_exactly_one_row_is_the_first_key() -> None:
    rows = list(load_byok_plan_catalog().values())
    first_key_rows = [row for row in rows if row.first_key is True]

    assert len(first_key_rows) == 1
    assert first_key_rows[0].customer_routable is True

    backend = first_key_backend()
    assert backend.first_key is True
    assert backend == first_key_rows[0]


def test_a_second_first_key_row_is_refused(tmp_path: Path) -> None:
    data = yaml.safe_load(Path(CATALOG_PATH).read_text(encoding="utf-8"))
    rows = data["providers"]

    second = next(row for row in rows if not row.get("first_key"))
    second["first_key"] = True

    out_path = tmp_path / "catalog.yaml"
    out_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ByokCatalogError, match="first_key"):
        _read_plan_catalog(out_path)


def test_a_default_model_outside_the_preferences_is_refused(tmp_path: Path) -> None:
    data = yaml.safe_load(Path(CATALOG_PATH).read_text(encoding="utf-8"))
    rows = data["providers"]
    first_row = rows[0]

    first_row["default_models"] = [
        *first_row.get("default_models", []),
        "not-a-listed-model",
    ]

    out_path = tmp_path / "catalog.yaml"
    out_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ByokCatalogError, match="default_models"):
        _read_plan_catalog(out_path)
