# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the scenario nodes' contracts and their settings overlays (OMN-23602).

R2, R3 and R5 failed for want of variables no one had created, while the keys they need already lived under other names in
the host's environment file. Each node's overlay now names those existing names. This test holds both sides: every variable a
contract reads for a key or an address is filled by its overlay (so a rename on either side is red), and a built runner holds
the key the overlay's existing name carries, without any variable set by hand. The failing control is the same build with the
existing name absent: the variable stays empty and the node's own check reports it, never a skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.delegation_scenario_settings import (
    load_settings_overlay,
    read_host_env_names,
)
from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioSettingsOverlay,
)
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers import (
    handler_scenario_r2 as r2,
)
from omnimarket.nodes.node_delegation_scenario_r2_effect.scenario_r2_settings import (
    setting_names,
)
from omnimarket.nodes.node_delegation_scenario_r3_effect.handlers import (
    handler_scenario_r3 as r3,
)
from omnimarket.nodes.node_delegation_scenario_r5_effect.handlers import (
    handler_scenario_r5 as r5,
)

pytestmark = pytest.mark.unit

FAKE_KEY = "fake-house-key-1"
NODES = Path(r3.__file__).resolve().parents[2]
R2_RUNTIME = r2.load_runtime()
R3_RUNTIME = r3.load_runtime()
R5_RUNTIME = r5.load_runtime()


def _overlay(node: str, relpath: str | None) -> ModelScenarioSettingsOverlay:
    assert relpath is not None, f"{node} declares no settings overlay"
    return load_settings_overlay(NODES / node / relpath)


def _home_with(tmp_path: Path, **names: str) -> Path:
    env_file = tmp_path / ".omnibase" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "".join(f"{k}={v}\n" for k, v in names.items()), encoding="utf-8"
    )
    return tmp_path


def _env(tmp_path: Path, runtime_home: str, append_env: str) -> dict[str, str]:
    return {runtime_home: str(tmp_path / "registry"), append_env: "true x"}


def test_r2_overlay_fills_every_setting_the_contract_reads() -> None:
    """Every one of R2's seven setting variables is filled by its overlay."""
    overlay = _overlay(
        "node_delegation_scenario_r2_effect", R2_RUNTIME.settings_overlay_relpath
    )

    assert {s.env for s in overlay.settings} == set(setting_names(R2_RUNTIME))
    assert overlay.folder == R2_RUNTIME.secret_store_folder


def test_r3_overlay_fills_the_probe_key_the_contract_reads() -> None:
    """R3's overlay fills the variable its contract names as the probe key."""
    overlay = _overlay(
        "node_delegation_scenario_r3_effect", R3_RUNTIME.settings_overlay_relpath
    )

    assert [s.env for s in overlay.settings] == [R3_RUNTIME.openrouter_probe_key_env]


def test_r5_overlay_fills_the_probe_key_the_contract_reads() -> None:
    """R5's overlay fills the variable its contract names as the probe key."""
    overlay = _overlay(
        "node_delegation_scenario_r5_effect", R5_RUNTIME.settings_overlay_relpath
    )

    assert [s.env for s in overlay.settings] == [R5_RUNTIME.openrouter_probe_key_env]


def test_r3_runner_holds_the_key_its_overlay_names(tmp_path: Path) -> None:
    """R3 built with only the existing OpenRouter name in the host file holds the probe key."""
    home = _home_with(tmp_path, OPENROUTER_API_KEY=FAKE_KEY)

    handler = r3.build_runner(
        _env(tmp_path, R3_RUNTIME.home_env, R3_RUNTIME.ledger_append_command_env),
        settings_home=home,
    )

    assert handler._environment[R3_RUNTIME.openrouter_probe_key_env] == FAKE_KEY


def test_r3_runner_without_the_existing_name_holds_no_key(tmp_path: Path) -> None:
    """The control: with the existing name absent the probe variable stays empty."""
    home = _home_with(tmp_path, UNRELATED_NAME="x")

    handler = r3.build_runner(
        _env(tmp_path, R3_RUNTIME.home_env, R3_RUNTIME.ledger_append_command_env),
        settings_home=home,
    )

    assert not handler._environment.get(R3_RUNTIME.openrouter_probe_key_env, "").strip()


def test_r5_runner_holds_the_key_its_overlay_names(tmp_path: Path) -> None:
    """R5 built with only the existing OpenRouter name in the host file holds the probe key."""
    home = _home_with(tmp_path, OPENROUTER_API_KEY=FAKE_KEY)

    handler = r5.build_runner(
        _env(tmp_path, R5_RUNTIME.home_env, R5_RUNTIME.ledger_append_command_env),
        settings_home=home,
    )

    assert handler._environment[R5_RUNTIME.openrouter_probe_key_env] == FAKE_KEY


def test_r5_runner_without_the_existing_name_holds_no_key(tmp_path: Path) -> None:
    """The control: with the existing name absent the probe variable stays empty."""
    home = _home_with(tmp_path, UNRELATED_NAME="x")

    handler = r5.build_runner(
        _env(tmp_path, R5_RUNTIME.home_env, R5_RUNTIME.ledger_append_command_env),
        settings_home=home,
    )

    assert not handler._environment.get(R5_RUNTIME.openrouter_probe_key_env, "").strip()


def test_only_the_named_values_are_read_from_the_host_file(tmp_path: Path) -> None:
    """Reading the host file for one name returns that name and no other value of the file."""
    home = _home_with(tmp_path, OPENROUTER_API_KEY=FAKE_KEY, DATABASE_URI="not-wanted")

    found = read_host_env_names(
        home, ".omnibase/.env", frozenset({"OPENROUTER_API_KEY"})
    )

    assert found == {"OPENROUTER_API_KEY": FAKE_KEY}
