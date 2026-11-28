# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus-session route resolves its lane in a runtime that mounts the declaration but exports nothing (OMN-24562).

Recorded dev runtime line, 16 times in 40 minutes: ``ledger rows command exited 69: READER reader=failure-alarm-router
query=rows status=unreadable source=none reason=node-read-failed-bus-session-failed-delegatelaneselectionerror-the-lane-decla``.
The container mounts ``config/lab/ci_bus_lanes.yaml`` under a config root it does not export, and holds the lane's SASL
identity in KAFKA_SASL_USERNAME / KAFKA_SASL_PASSWORD with no client credential store. Both are the overlay's and the
environment's to name, never a host path in code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.model_work_ledger_reader import ModelBusSessionReadRoute
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    resolve_bus_session_config,
)

pytestmark = pytest.mark.unit

DECLARATION = """\
default: inmemory
lanes:
  dev:
    broker: "broker.example:19092"
    security_protocol: SASL_PLAINTEXT
    sasl_mechanism: SCRAM-SHA-256
"""
SECRET = "fixture-credential"


@pytest.fixture
def config_root(tmp_path: Path) -> Path:
    lab = tmp_path / "config" / "lab"
    lab.mkdir(parents=True)
    (lab / "ci_bus_lanes.yaml").write_text(DECLARATION, encoding="utf-8")
    return tmp_path


def _route(root: Path | None) -> ModelBusSessionReadRoute:
    return ModelBusSessionReadRoute(
        kind="bus-session",
        bus_lane="dev",
        timeout_seconds=30,
        workspace_config_root=str(root) if root else None,
    )


def test_a_mounted_config_root_named_by_the_route_resolves_the_lane_with_env_identity(
    config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    env = {
        "OMNI_HOME": str(tmp_path / "empty"),
        "KAFKA_SASL_USERNAME": "reader",
        "KAFKA_SASL_PASSWORD": SECRET,
    }
    config = resolve_bus_session_config(_route(config_root), env)
    assert config.bootstrap_servers == "broker.example:19092"
    assert config.sasl_plain_username == "reader"
    assert str(config.sasl_plain_password) == SECRET


def test_the_variable_is_put_back_once_the_lane_is_resolved(
    config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "empty"))
    env = {
        "OMNI_HOME": str(tmp_path / "empty"),
        "KAFKA_SASL_USERNAME": "reader",
        "KAFKA_SASL_PASSWORD": SECRET,
    }
    resolve_bus_session_config(_route(config_root), env)
    assert "ONEX_WORKSPACE_CONFIG_ROOT" not in os.environ


def test_control_no_declared_root_and_none_in_the_environment_refuses_naming_the_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "empty"))
    with pytest.raises(Exception, match="lane declaration"):
        resolve_bus_session_config(_route(None), {"OMNI_HOME": str(tmp_path / "empty")})
