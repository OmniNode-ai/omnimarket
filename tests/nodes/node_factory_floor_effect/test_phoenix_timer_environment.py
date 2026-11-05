# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installed floor timer carries the Phoenix URL the floor reads its traces from.

launchd does not inherit the installing shell's environment. Before this, the factory-floor-publish unit declared no
Phoenix variable, so every timer tick read the floor with the variable unset and the machinery panel drew the
Phoenix row as "no Phoenix reading was supplied": the floor could never show whether traces reach Phoenix.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

pytestmark = pytest.mark.unit

FLOOR_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_effect/contract.yaml"
)
UNIT = "factory-floor-publish"


def _phoenix_env_name() -> str:
    contract = yaml.safe_load(FLOOR_CONTRACT.read_text(encoding="utf-8"))
    name = contract["runtime"]["phoenix_url_env"]
    assert isinstance(name, str)
    return name


def _timer_environment(tmp_path: Path, **fields: Any) -> dict[str, str]:
    request = ModelRegistryUnitsRequest(
        unit=UNIT,
        home=str(tmp_path),
        registry_root=str(tmp_path),
        workspace_config_root=str(tmp_path),
        plans_path=str(tmp_path),
        marketplace_root=str(tmp_path),
        runtime_ssh_host="lab-runtime",
        **fields,
    )
    handler = HandlerRegistryUnits(
        lambda argv: subprocess.CompletedProcess(argv, 0, "test-user", "")
    )
    text = handler.render(request, load_units()[UNIT], tmp_path, "uv", "python")
    environment: dict[str, str] = plistlib.loads(text.encode())["EnvironmentVariables"]
    return environment


def test_the_timer_persists_the_phoenix_url_the_floor_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _phoenix_env_name()
    monkeypatch.delenv(name, raising=False)
    environment = _timer_environment(tmp_path, phoenix_url="http://phoenix.lab:6006")
    assert environment[name] == "http://phoenix.lab:6006"


def test_an_exported_phoenix_url_is_persisted_when_the_request_names_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _phoenix_env_name()
    monkeypatch.setenv(name, "http://exported-phoenix:6006")
    assert _timer_environment(tmp_path)[name] == "http://exported-phoenix:6006"


def test_without_a_phoenix_url_the_timer_sets_none_and_the_floor_draws_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _phoenix_env_name()
    monkeypatch.delenv(name, raising=False)
    assert name not in _timer_environment(tmp_path)


@pytest.mark.parametrize("url", ["ftp://phoenix", "http://a b", "phoenix:6006"])
def test_a_phoenix_url_that_is_not_http_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="phoenix_url"):
        ModelRegistryUnitsRequest(
            unit=UNIT, home="/h", registry_root="/r", phoenix_url=url
        )
