# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe tenant key of scenario R8 is resolved from the secret store through the settings overlay.

The golden run proves the key the store holds reaches the handler's environment and never leaks into the
serialized result; the control run proves a store that holds no key leaves the key unset, so the http lane
build refuses it. The committed overlay file is validated and names exactly the one setting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.handlers.delegation_scenario_settings import (
    ProtocolSettingStore,
    load_settings_overlay,
)
from omnimarket.models.model_delegation_scenario import ModelScenarioResult
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    InMemoryLane,
    LaneReadError,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    CONTRACT_PATH,
    LANE_BUILDERS,
    ModelScenarioR8RunCommand,
    build_runner,
    load_lane_settings,
    load_runtime,
)

KEY_NAME = "R8_PROBE_TENANT_API_KEY"
KEY_VALUE = "probe-key-value"


class _FakeStore(ProtocolSettingStore):
    """A store that answers the probe key from a fixed folder/name pair and nothing else."""

    def __init__(self, values: dict[tuple[str, str], str]) -> None:
        self._values = values

    async def read(self, folder: str, name: str) -> SecretStr | None:
        value = self._values.get((folder, name))
        return SecretStr(value) if value is not None else None


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "OMNI_HOME": str(tmp_path),
        "ONEX_LEDGER_BUS_APPEND_COMMAND": "true",
        "R8_PROBE_API_BASE_URL": "http://127.0.0.1:9",
    }


def test_golden_key_reaches_environment_and_never_leaks(tmp_path: Path) -> None:
    store = _FakeStore({("/shared/env", KEY_NAME): KEY_VALUE})
    record = tmp_path / "rows.txt"
    handler = build_runner(
        _env(tmp_path),
        settings_store=store,
        settings_home=tmp_path,
        record=record,
    )
    assert handler._environment.get(KEY_NAME) == KEY_VALUE

    handler._lane_factory = lambda: InMemoryLane()
    result = handler.handle(ModelScenarioR8RunCommand())
    assert isinstance(result, ModelScenarioResult)
    serialized = json.dumps(result.model_dump(mode="json"))
    assert KEY_VALUE not in serialized
    assert KEY_VALUE not in record.read_text(encoding="utf-8")
    journal = tmp_path / load_runtime().event_journal_relpath
    assert KEY_VALUE not in journal.read_text(encoding="utf-8")


def test_control_missing_key_refuses_http_lane(tmp_path: Path) -> None:
    store = _FakeStore({})
    handler = build_runner(
        _env(tmp_path),
        settings_store=store,
        settings_home=tmp_path,
    )
    assert handler._environment.get(KEY_NAME) is None

    settings = load_lane_settings()
    with pytest.raises(LaneReadError) as excinfo:
        LANE_BUILDERS["http"](settings, handler._environment)
    assert KEY_NAME in str(excinfo.value)


def test_committed_overlay_names_exactly_one_setting() -> None:
    overlay = load_settings_overlay(
        CONTRACT_PATH.parent / "inputs/settings_overlay.yaml"
    )
    assert overlay.folder == "/shared/env"
    assert len(overlay.settings) == 1
    setting = overlay.settings[0]
    assert setting.env == KEY_NAME
    assert setting.secret == KEY_NAME
