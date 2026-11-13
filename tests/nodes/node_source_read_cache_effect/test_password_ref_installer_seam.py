# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the Valkey password reference (OMN-24112): installer to cache, and cache to the secret store reader.

Installer to cache: the unit's contract maps the environment variable to the request field, and every reference the request
accepts is accepted by the cache's ``split_store_reference`` while every reference it refuses is refused there too. The two
sides must agree, so the test fails when either side's accepted shape changes.
Cache to the secret store reader: the cache calls ``SecretStoreSettingsReader.read(folder, name)`` (an awaitable returning a
secret or nothing); a change of that signature fails here before it fails a board cycle.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.delegation_scenario_settings import SecretStoreSettingsReader
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.password_reference import (
    split_store_reference,
)

pytestmark = pytest.mark.unit

UNIT = "factory-floor-publish"
ENV_VAR = "ONEX_SOURCE_CACHE_VALKEY_PASSWORD_REF"
FIELD = "source_cache_valkey_password_ref"

VALID = (
    "secret://onex/shared/valkey/VALKEY_PASSWORD",
    "secret://onex/KEY",
    "secret://onex/a.b/c-d/E_F",
    "secret://onex/x/Y1",
)
INVALID = (
    "hunter2",
    "secret://onex/",
    "secret://onex/a b/K",
    "secret://other/a/B",
    "secret://onex/a;b/K",
)


def _request(tmp_path: Path, reference: str) -> ModelRegistryUnitsRequest:
    return ModelRegistryUnitsRequest(
        unit=UNIT,
        home=str(tmp_path),
        registry_root=str(tmp_path),
        source_cache_valkey_password_ref=reference,
    )


def test_the_unit_maps_the_variable_to_the_request_field() -> None:
    unit = load_units()[UNIT]
    assert unit["environment_inputs"][ENV_VAR] == FIELD
    assert FIELD in ModelRegistryUnitsRequest.model_fields


@pytest.mark.parametrize("reference", VALID)
def test_every_reference_the_request_accepts_is_accepted_by_the_cache(
    tmp_path: Path, reference: str
) -> None:
    assert _request(tmp_path, reference).source_cache_valkey_password_ref == reference
    split_store_reference(reference)


@pytest.mark.parametrize("reference", INVALID)
def test_every_reference_the_request_refuses_is_refused_by_the_cache(
    tmp_path: Path, reference: str
) -> None:
    with pytest.raises(ValidationError):
        _request(tmp_path, reference)
    with pytest.raises(ValueError, match="secret store reference"):
        split_store_reference(reference)


def test_the_secret_store_reader_keeps_the_signature_the_cache_calls() -> None:
    read = SecretStoreSettingsReader.read
    assert inspect.iscoroutinefunction(read)
    assert list(inspect.signature(read).parameters) == ["self", "folder", "name"]
