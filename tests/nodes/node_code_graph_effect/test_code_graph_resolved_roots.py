# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The code graph lookup finds its overlay and its clones on a host that sets neither variable.

A host whose overlay roots hold only the plugin's overlay folder, or whose clones root variable is
unset, answered ``NodeOverlayNotFoundError`` or a refusal. The node's overlay load resolves its
roots through the overlay root resolver and its clones root falls back to the registry root.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.nodes.node_code_graph_effect import runtime_code_graph
from omnimarket.nodes.node_code_graph_effect.overlay import load_overlay
from omnimarket.nodes.node_code_graph_effect.runtime_code_graph import main
from tests.nodes.node_code_graph_effect.code_graph_support import (
    APP,
    CORE,
    EXAMPLE,
    make_clone,
)

pytestmark = pytest.mark.unit

CLONES_VAR = "EXAMPLE_REPOS_ROOT"


def _write_overlay(root: Path) -> Path:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["graph_backend"] = "git"
    target = root / "node_code_graph_effect" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    return target


def _host(tmp_path: Path) -> tuple[Path, Path]:
    """A registry root with the canonical overlays clone beside it, and a plugin folder with no overlay."""
    registry = tmp_path / "registry"
    registry.mkdir()
    _write_overlay(tmp_path / "overlays" / "overlays")
    plugin = tmp_path / "plugin" / "overlays"
    plugin.mkdir(parents=True)
    return registry, plugin


def _lookup(capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    runtime_code_graph._VERSIONS.clear()
    code = main(
        [
            "lookup",
            "--question",
            "importers",
            "--repo",
            "example_core",
            "--path",
            "src/example_core/models.py",
        ]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_the_canonical_clone_supplies_the_overlay_when_the_declared_roots_hold_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, plugin = _host(tmp_path)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(plugin))
    monkeypatch.setenv("OMNIBASE_PATH", str(registry))
    assert load_overlay().repos_root_env == CLONES_VAR


def test_the_lookup_answers_with_only_the_plugin_folder_declared_and_the_clones_root_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry, plugin = _host(tmp_path)
    make_clone(registry, "example_core", CORE)
    make_clone(registry, "example_app", APP)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(plugin))
    monkeypatch.setenv("OMNIBASE_PATH", str(registry))
    code, out, err = _lookup(capsys)
    assert code == 0, err
    assert "MATCH src/example_core/cli.py\n" in out
    assert "status=found" in out.splitlines()[-1]


def test_omni_home_names_the_registry_root_when_omnibase_path_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry, plugin = _host(tmp_path)
    make_clone(registry, "example_core", CORE)
    make_clone(registry, "example_app", APP)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(plugin))
    monkeypatch.setenv("OMNI_HOME", str(registry))
    code, _, err = _lookup(capsys)
    assert code == 0, err


def test_the_explicit_clones_root_wins_over_the_registry_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry, plugin = _host(tmp_path)
    explicit = tmp_path / "explicit_clones"
    make_clone(explicit, "example_core", CORE)
    make_clone(explicit, "example_app", APP)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(plugin))
    monkeypatch.setenv("OMNIBASE_PATH", str(registry))
    monkeypatch.setenv(CLONES_VAR, str(explicit))
    code, out, err = _lookup(capsys)
    assert code == 0, err
    assert "MATCH src/example_core/cli.py\n" in out


def test_a_declared_root_answers_where_no_earlier_root_holds_an_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, plugin = _host(tmp_path)
    declared = tmp_path / "declared"
    target = _write_overlay(declared)
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    document["repos_root_env"] = "DECLARED_REPOS_ROOT"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", f"{plugin}:{declared}")
    monkeypatch.setenv("OMNIBASE_PATH", str(registry))
    # The canonical clone is searched before the declared roots, so it answers here; a declared
    # root answers only where no earlier root holds the overlay.
    assert load_overlay().repos_root_env == CLONES_VAR
    monkeypatch.setenv(
        "OMNIBASE_PATH", str(tmp_path / "elsewhere" / "no_such_registry")
    )
    assert load_overlay().repos_root_env == "DECLARED_REPOS_ROOT"


def test_no_root_holding_an_overlay_refuses_with_the_typed_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "plugin" / "overlays"
    plugin.mkdir(parents=True)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(plugin))
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path / "registry"))
    with pytest.raises(NodeOverlayNotFoundError) as refused:
        load_overlay()
    message = str(refused.value)
    assert "node_code_graph_effect: the overlay file is not configured" in message
    assert str(plugin) in message


def test_an_unset_clones_root_with_no_registry_root_names_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    overlays = tmp_path / "overlays"
    _write_overlay(overlays)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlays))
    code, _, err = _lookup(capsys)
    assert code == 3
    assert f"{CLONES_VAR} is not set" in err
    assert "OMNIBASE_PATH" in err
    assert "OMNI_HOME" in err
