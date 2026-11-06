# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installed floor timer carries the path of the alert credentials file and the overlay roots it was told (OMN-19998).

launchd does not inherit the installing shell's environment, so the lab-state alarm can reach its Slack credentials only
when the unit persists the path of the operator's bootstrap env file. Only the path is persisted, never a value. The
overlay roots may be extended by the request with the private overlay home, after the roots the unit already had.
"""

from __future__ import annotations

import plistlib
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

UNIT = "factory-floor-publish"
NAME = "ONEX_ALERT_CHANNEL_ENV_FILE"


def timer_environment(tmp_path: Path, **fields: Any) -> dict[str, str]:
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


def test_golden_the_timer_persists_the_path_of_the_alert_credentials_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(NAME, raising=False)
    environment = timer_environment(tmp_path, alert_env_file="/etc/operator/alerts.env")
    assert environment[NAME] == "/etc/operator/alerts.env"


def test_golden_the_overlay_roots_can_be_extended_with_the_private_overlay_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(NAME, raising=False)
    roots = f"{tmp_path}/plugin/overlays:{tmp_path}/private/overlays"
    environment = timer_environment(
        tmp_path, path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": roots}
    )
    assert environment["ONEX_SKILL_OVERLAY_ROOTS"] == roots


def test_error_without_the_path_the_timer_sets_none_and_a_slack_alarm_is_refused_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(NAME, raising=False)
    assert NAME not in timer_environment(tmp_path)


def test_error_a_value_that_is_not_an_absolute_path_is_refused_so_no_secret_can_ride_in_it() -> (
    None
):
    with pytest.raises(ValueError, match="alert_env_file"):
        ModelRegistryUnitsRequest(
            unit=UNIT,
            home="/h",
            registry_root="/r",
            alert_env_file="xoxb-looks-like-a-token value",
        )
