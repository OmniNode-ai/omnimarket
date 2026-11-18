# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor publish timer carries the reference of the Valkey password the board's source read cache uses (OMN-24112).

launchd does not inherit the installing shell's environment, so a unit that does not persist the reference the cache overlay names
leaves the cache unable to authenticate for every cycle. Golden: a request that supplies the reference renders it, and the installing
shell supplies it when the request has none. Error chain: a request with no reference renders none, and a reference that is not a store
path is refused by the model. No password value is a request input, so a dry run's printed unit never carries one.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

pytestmark = pytest.mark.unit

UNIT = "factory-floor-publish"


def _render(tmp_path: Path, **inputs: str) -> str:
    request = ModelRegistryUnitsRequest(
        unit=UNIT,
        home=str(tmp_path),
        registry_root=str(tmp_path / "registry"),
        workspace_config_root=str(tmp_path / "workspace-config"),
        plans_path=str(tmp_path),
        marketplace_root=str(tmp_path),
        runtime_ssh_host="lab-runtime",
        ledger_host_name="lab-host",
        **inputs,
    )
    handler = HandlerRegistryUnits(
        lambda argv: subprocess.CompletedProcess(argv, 0, "test-user", "")
    )
    return handler.render(request, load_units()[UNIT], tmp_path, "uv", "python")


def _environment(tmp_path: Path, **inputs: str) -> dict[str, str]:
    environment: dict[str, str] = plistlib.loads(_render(tmp_path, **inputs).encode())[
        "EnvironmentVariables"
    ]
    return environment


def test_golden_the_rendered_unit_carries_the_password_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SOURCE_CACHE_VALKEY_HOST", raising=False)
    monkeypatch.delenv("ONEX_SOURCE_CACHE_VALKEY_PORT", raising=False)
    monkeypatch.delenv("ONEX_SOURCE_CACHE_VALKEY_PASSWORD_REF", raising=False)
    environment = _environment(
        tmp_path,
        source_cache_valkey_password_ref="secret://onex/shared/valkey/VALKEY_PASSWORD",
    )
    assert environment["ONEX_SOURCE_CACHE_VALKEY_PASSWORD_REF"] == (
        "secret://onex/shared/valkey/VALKEY_PASSWORD"
    )
    assert not any(
        "PASSWORD" in name
        for name in environment
        if name != "ONEX_SOURCE_CACHE_VALKEY_PASSWORD_REF"
    )
    text = _render(
        tmp_path,
        source_cache_valkey_password_ref=environment[
            "ONEX_SOURCE_CACHE_VALKEY_PASSWORD_REF"
        ],
    )
    assert "VALKEY_PASSWORD=" not in text


def test_golden_the_installing_shell_supplies_the_reference_when_the_request_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SOURCE_CACHE_VALKEY_HOST", raising=False)
    monkeypatch.delenv("ONEX_SOURCE_CACHE_VALKEY_PORT", raising=False)
    monkeypatch.setenv(
        "ONEX_SOURCE_CACHE_VALKEY_PASSWORD_REF",
        "secret://onex/shared/valkey/VALKEY_PASSWORD",
    )
    environment = _environment(tmp_path)
    assert environment["ONEX_SOURCE_CACHE_VALKEY_PASSWORD_REF"] == (
        "secret://onex/shared/valkey/VALKEY_PASSWORD"
    )


def test_error_a_request_with_no_reference_renders_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SOURCE_CACHE_VALKEY_HOST", raising=False)
    monkeypatch.delenv("ONEX_SOURCE_CACHE_VALKEY_PORT", raising=False)
    monkeypatch.delenv("ONEX_SOURCE_CACHE_VALKEY_PASSWORD_REF", raising=False)
    environment = _environment(tmp_path)
    assert "ONEX_SOURCE_CACHE_VALKEY_PASSWORD_REF" not in environment


def test_error_a_reference_that_is_not_a_store_path_is_refused(tmp_path: Path) -> None:
    good = "secret://onex/shared/valkey/VALKEY_PASSWORD"
    assert _environment(tmp_path, source_cache_valkey_password_ref=good)
    for bad in (
        "hunter2",
        "secret://onex/",
        "secret://other/shared/x",
        "secret://onex/shared/a b",
        "secret://onex/shared/k;rm",
    ):
        with pytest.raises(ValidationError):
            _environment(tmp_path, source_cache_valkey_password_ref=bad)


def test_a_password_value_is_not_a_request_field() -> None:
    assert not any(
        name.endswith("password") for name in ModelRegistryUnitsRequest.model_fields
    )
