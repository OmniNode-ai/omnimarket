"""OMN-26617 reproduction (adapted from records/evidence)."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.nodes.node_onex_setup_effect import cli_delegation
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)

pytestmark = pytest.mark.unit

INSTALL_HINT = (
    "Install or repair the packaged delegation hook assets with onex install "
    "omninode-claude and check the selected Claude settings, then retry setup."
)


class _Step:
    def __init__(self, observation: ModelSetupObservation) -> None:
        self._observation = observation

    def observe(self, plan: object) -> ModelSetupObservation:
        return self._observation

    def apply(self, plan: object) -> ModelSetupObservation | None:
        return None

    def developer_registered(self) -> bool:
        return False


def _obs(step: str, status: str, fix: str | None = None) -> ModelSetupObservation:
    return ModelSetupObservation(step=step, status=status, fix=fix)


def _run_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, switch: bool):
    plugin = _obs("claude-plugin", "success")
    hooks = _obs("claude-hooks", "action_required", INSTALL_HINT)
    monkeypatch.setattr(
        cli_delegation,
        "HandlerSetupClaude",
        lambda name, _replace=False: _Step(
            plugin if name == "claude-plugin" else hooks
        ),
    )
    monkeypatch.setattr(cli_delegation, "delegation_switch_enabled", lambda: switch)
    monkeypatch.setattr(
        cli_delegation, "read_switches", lambda: type("S", (), {"prompts": True})()
    )
    monkeypatch.setattr(cli_delegation, "write_switches", lambda _switches: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    text = CliRunner().invoke(cli_delegation.delegation_group, ["status"])
    document = json.loads(
        CliRunner()
        .invoke(cli_delegation.delegation_group, ["status", "--json"])
        .output.strip()
        .splitlines()[-1]
    )
    return text.output, document


def test_ac1_off_and_ready_names_onex_delegation_on_not_an_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text, document = _run_status(monkeypatch, tmp_path, switch=False)
    assert document["status"] == "off"
    assert document["claude_code"] == "ready"
    assert "onex delegation on" in text
    assert "onex install" not in text
    assert "onex delegation on" in str(document["fix"])
    assert "onex install" not in str(document["fix"])


def test_ac2_assets_really_broken_keeps_the_repair_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text, document = _run_status(monkeypatch, tmp_path, switch=True)
    assert "onex install" in text
    assert "onex install" in str(document["fix"])
