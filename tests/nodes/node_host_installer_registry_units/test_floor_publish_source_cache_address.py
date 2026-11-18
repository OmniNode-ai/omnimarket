# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor publish timer carries the address of the Valkey the board's source read cache uses (OMN-24050).

launchd does not inherit the installing shell's environment, so a unit that does not persist the host and port the cache overlay names
leaves the cache off for every cycle. Golden: a request that supplies them renders both variables. Error chain: a request that supplies
neither renders neither (the cache is off and the steps read their sources), and a request with a host that is not a host name is refused
by the model. No password is a request input, so a dry run's printed unit never carries one.
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


def _environment(tmp_path: Path, **inputs: str) -> dict[str, str]:
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
    text = handler.render(request, load_units()[UNIT], tmp_path, "uv", "python")
    environment: dict[str, str] = plistlib.loads(text.encode())["EnvironmentVariables"]
    return environment


def test_golden_the_rendered_unit_carries_the_cache_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SOURCE_CACHE_VALKEY_HOST", raising=False)
    monkeypatch.delenv("ONEX_SOURCE_CACHE_VALKEY_PORT", raising=False)
    environment = _environment(
        tmp_path,
        source_cache_valkey_host="valkey.example.invalid",
        source_cache_valkey_port="16379",
    )
    assert environment["ONEX_SOURCE_CACHE_VALKEY_HOST"] == "valkey.example.invalid"
    assert environment["ONEX_SOURCE_CACHE_VALKEY_PORT"] == "16379"


def test_error_a_request_with_no_address_renders_none_and_the_unit_has_no_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SOURCE_CACHE_VALKEY_HOST", raising=False)
    monkeypatch.delenv("ONEX_SOURCE_CACHE_VALKEY_PORT", raising=False)
    environment = _environment(tmp_path)
    assert "ONEX_SOURCE_CACHE_VALKEY_HOST" not in environment
    assert "ONEX_SOURCE_CACHE_VALKEY_PORT" not in environment
    assert not any("PASSWORD" in name for name in environment)


def test_error_a_host_that_is_not_a_host_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _environment(tmp_path, source_cache_valkey_host="bad host;rm")
