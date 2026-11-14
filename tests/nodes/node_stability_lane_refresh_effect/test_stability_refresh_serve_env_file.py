# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refresh serve unit loads the credential file the service principal mint delivers.

The stability-refresh-serve-systemd unit declares an environment_file relative to the home; the service principal
mint writes the credential into that file, and the unit loads it with a leading dash so a host without the file
still runs. Either side changing shape turns this red. OMN-24314.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

pytestmark = pytest.mark.unit

UNIT = "stability-refresh-serve-systemd"
ENVIRONMENT_FILE = ".config/onex/stability-refresh-serve.env"
CREDENTIAL_KEYS = ("KAFKA_SASL_USERNAME", "KAFKA_SASL_PASSWORD")


def _request(tmp_path: Path) -> ModelRegistryUnitsRequest:
    return ModelRegistryUnitsRequest(
        unit=UNIT,
        home=str(tmp_path),
        registry_root=str(tmp_path),
        workspace_config_root=str(tmp_path),
        plans_path=str(tmp_path),
        marketplace_root=str(tmp_path),
        runtime_ssh_host="lab-runtime",
    )


def _handler() -> HandlerRegistryUnits:
    return HandlerRegistryUnits(
        lambda argv: subprocess.CompletedProcess(argv, 0, "test-user", "")
    )


def _rendered_service(tmp_path: Path, unit: dict[str, Any]) -> str:
    service, _timer = _handler().render_systemd(
        _request(tmp_path), unit, tmp_path, "uv", "python"
    )
    return service


def test_the_rendered_unit_loads_the_optional_stability_credential_file(
    tmp_path: Path,
) -> None:
    text = _rendered_service(tmp_path, load_units()[UNIT])
    assert f"EnvironmentFile=-{tmp_path}/{ENVIRONMENT_FILE}" in text


def test_the_unit_declares_the_file_relative_to_the_home() -> None:
    unit = load_units()[UNIT]
    declared = unit["environment_file"]
    assert declared == ENVIRONMENT_FILE
    assert not Path(declared).is_absolute()


def test_without_the_declaration_the_rendered_unit_carries_no_environment_file_line(
    tmp_path: Path,
) -> None:
    unit = dict(load_units()[UNIT])
    del unit["environment_file"]
    text = _rendered_service(tmp_path, unit)
    assert "EnvironmentFile=" not in text


def test_the_unit_environment_declares_no_credential_variable() -> None:
    unit = load_units()[UNIT]
    environment = unit.get("environment") or {}
    assert not any(key in CREDENTIAL_KEYS for key in environment)
