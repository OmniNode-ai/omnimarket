"""OMN-26617 reproduction (adapted from records/evidence)."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.nodes.node_onex_setup_effect import cli_delegation
from omnimarket.nodes.node_onex_setup_effect.handlers import handler_setup_claude
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)

pytestmark = pytest.mark.unit

#: The handler's shared constant once it exists; the message the handler has always
#: printed before that, so these tests collect and fail on behaviour at an older base.
INSTALL_HINT: str = getattr(
    handler_setup_claude,
    "HOOK_ASSETS_UNREADABLE_FIX",
    "Install or repair the packaged delegation hook assets with onex install "
    "omninode-claude and check the selected Claude settings, then retry setup.",
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


class _Switches:
    prompts = True


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
    monkeypatch.setattr(cli_delegation, "read_switches", _Switches)
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


def _break_the_hook_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str
) -> None:
    """Make the real hooks step find really missing or broken hook assets.

    The hooks step is the real handler, not a scripted observation: the customer
    asset resolver names an installer, and the assets it names are then removed
    ("installer": the installer file is gone) or broken ("bundle": the installer is
    there but the hook bundle beside it is gone).
    """
    bundle = tmp_path / "customer-assets"
    installer = bundle / "scripts" / "install.py"
    installer.parent.mkdir(parents=True)
    installer.write_text("# installer\n")
    if how == "installer":
        installer.unlink()
    assert not (bundle / "plugins" / "onex").exists()
    monkeypatch.setattr(
        handler_setup_claude.HandlerSetupClaude,
        "_customer_assets",
        staticmethod(lambda: {"installer": str(installer)}),
    )


def _run_status_with_real_hooks_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, switch: bool
):
    plugin = _obs("claude-plugin", "success")
    real = handler_setup_claude.HandlerSetupClaude
    monkeypatch.setattr(
        cli_delegation,
        "HandlerSetupClaude",
        lambda name, _replace=False: (
            _Step(plugin) if name == "claude-plugin" else real(name="claude-hooks")
        ),
    )
    monkeypatch.setattr(cli_delegation, "delegation_switch_enabled", lambda: switch)
    monkeypatch.setattr(cli_delegation, "read_switches", _Switches)
    monkeypatch.setattr(cli_delegation, "write_switches", lambda _switches: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ONEX_HOOKS_MASK", raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    text = CliRunner().invoke(cli_delegation.delegation_group, ["status"])
    document = json.loads(
        CliRunner()
        .invoke(cli_delegation.delegation_group, ["status", "--json"])
        .output.strip()
        .splitlines()[-1]
    )
    return text.output, document


@pytest.mark.parametrize("how", ["installer", "bundle"])
def test_ac2_assets_really_removed_keeps_the_repair_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str
) -> None:
    """AC2: with the hook assets really removed (or broken) and delegation wanted
    (the switch on), status still names the repair command, not a plain on."""
    _break_the_hook_assets(monkeypatch, tmp_path, how)
    text, document = _run_status_with_real_hooks_step(
        monkeypatch, tmp_path, switch=True
    )
    assert "onex install omninode-claude" in text
    assert "onex install omninode-claude" in str(document["fix"])
    assert "Run onex delegation on" not in text


@pytest.mark.parametrize("how", ["installer", "bundle"])
def test_assets_removed_with_delegation_off_leads_with_onex_delegation_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str
) -> None:
    """The boundary the approved spec chose, kept visible: the reading the Mac showed
    (the unreadable-assets text with the switch off) is told from a break by the
    switch alone, so even really removed assets lead with `onex delegation on` while
    delegation is off. `onex delegation on` then fails and names the repair (the next
    test). Whether that is the wanted reading of AC2 is the ticket owner's decision;
    if the answer is "repair first, always", delete this test and the switch term in
    cli_delegation._report."""
    _break_the_hook_assets(monkeypatch, tmp_path, how)
    text, document = _run_status_with_real_hooks_step(
        monkeypatch, tmp_path, switch=False
    )
    assert document["status"] == "off"
    assert "onex delegation on" in text
    assert "onex install" not in str(document["fix"])


def test_on_that_fails_to_install_names_the_repair_not_healthy_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`onex delegation on` with really broken assets must not read as healthy off."""
    plugin = _obs("claude-plugin", "success")
    hooks = _obs("claude-hooks", "action_required", INSTALL_HINT)
    monkeypatch.setattr(
        cli_delegation,
        "HandlerSetupClaude",
        lambda name, _replace=False: _Step(
            plugin if name == "claude-plugin" else hooks
        ),
    )
    monkeypatch.delenv("ONEX_HOOKS_MASK", raising=False)
    monkeypatch.setattr(cli_delegation, "delegation_switch_enabled", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    result = CliRunner().invoke(cli_delegation.delegation_group, ["on"])
    assert result.exit_code == 1
    assert "onex install omninode-claude" in result.output
    assert "Run onex delegation on" not in result.output
