# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host drift owner step is a declared systemd unit that names its runner, so no one sets the dispatch command by hand (OMN-24239)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units_systemd import (
    HANDLER,
    LINUX_PYTHON,
    Recorder,
    _git,
)

UNIT = "host-drift-owner-dispatch-systemd"
LABEL = "onex-host-drift-owner-dispatch"
ENTRY = "src/omnimarket/nodes/node_host_drift_owner_dispatch_effect/runtime_host_drift_owner_dispatch.py"


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def checkout(path: Path) -> str:
    path.mkdir(parents=True)
    _git(["git", "-C", str(path), "init", "-q", "-b", "branch"], check=True)
    (path / ENTRY).parent.mkdir(parents=True)
    (path / ENTRY).write_text("print('tick')\n", encoding="utf-8")
    _git(["git", "-C", str(path), "add", "-A"], check=True)
    _git(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "x",
        ],
        check=True,
    )
    return _git(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def request(
    tmp_path: Path, path: Path, revision: str, **kw: object
) -> ModelRegistryUnitsRequest:
    fields: dict[str, object] = {
        "unit": UNIT,
        "home": str(tmp_path / "home"),
        "registry_root": str(tmp_path / "omni"),
        "workspace_config_root": str(tmp_path / "ws"),
        "remote_lane_runner": "/example/plugin/remote-lane/onex_remote_lane.py",
        "ledger_host_name": "h-example",
        "ledger_path": "/example/ledger/rows.md",
        "marketplace_root": "/example/marketplace",
        "project_path": str(path),
        "expected_revision": revision,
        "path_overrides": {
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays")
        },
    }
    fields.update(kw)
    return ModelRegistryUnitsRequest.model_validate(fields)


def test_the_unit_declares_a_run_limit_under_its_interval_and_the_nodes_timeout() -> (
    None
):
    declared = load_units()[UNIT]
    assert declared["platform"] == "systemd"
    assert (
        0
        < declared["schedule"]["run_timeout_s"]
        <= declared["schedule"]["start_interval"]
    )
    assert declared["schedule"]["run_timeout_s"] * 1000 <= 600000


def test_the_service_runs_the_owner_step_and_names_the_runner(tmp_path: Path) -> None:
    path = tmp_path / "branch"
    revision = checkout(path)
    result = HandlerRegistryUnits(Recorder()).handle(request(tmp_path, path, revision))
    assert result.status == "success", result.stderr
    assert (
        '"-m" "omnimarket.nodes.node_host_drift_owner_dispatch_effect.runtime_host_drift_owner_dispatch"'
        in result.rendered
    )
    runner = "/example/plugin/remote-lane/onex_remote_lane.py"
    assert f'"ONEX_HOST_DRIFT_OWNER_RUNNER={runner}"' in result.rendered
    assert "TimeoutStartSec=600" in result.rendered
    assert "OnUnitActiveSec=900" in result.rendered
    assert f"Unit={LABEL}.service" in result.rendered


def test_the_runner_variable_is_the_one_the_nodes_overlay_names() -> None:
    from omnimarket.nodes.node_host_drift_owner_dispatch_effect.overlay import NODE_NAME

    declared = load_units()[UNIT]
    assert NODE_NAME in declared["module"]
    assert (
        declared["environment_inputs"]["ONEX_HOST_DRIFT_OWNER_RUNNER"]
        == "remote_lane_runner"
    )
    assert len(declared["required_environment_inputs"]) == 3


def test_an_install_that_names_no_runner_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_HOST_DRIFT_OWNER_RUNNER", raising=False)
    path = tmp_path / "branch"
    revision = checkout(path)
    result = HandlerRegistryUnits(Recorder()).handle(
        request(tmp_path, path, revision, mode="install", remote_lane_runner="")
    )
    assert result.status == "failure"
    assert "ONEX_HOST_DRIFT_OWNER_RUNNER" in result.stderr


def test_the_runner_gets_the_ledger_write_environment_the_checkers_own_unit_has(
    tmp_path: Path,
) -> None:
    path = tmp_path / "branch"
    revision = checkout(path)
    result = HandlerRegistryUnits(Recorder()).handle(request(tmp_path, path, revision))
    assert result.status == "success", result.stderr
    assert '"ONEX_LEDGER_WRITE_VIA=bus"' in result.rendered
    assert '"ONEX_LEDGER_HOST_NAME=h-example"' in result.rendered
    assert "=/example/ledger/rows.md" in result.rendered
    assert "omnimarket.work_ledger_bus.cli append --bus-lane dev" in result.rendered


def test_the_unit_holds_no_linear_key_and_the_nodes_overlay_names_the_store_entry() -> (
    None
):
    from tests.nodes.node_host_drift_owner_dispatch_effect.support import overlay

    declared = load_units()[UNIT]
    carried = {
        *declared["environment"],
        *declared["environment_inputs"],
        *declared["required_environment_inputs"],
    }
    assert not any("LINEAR" in name or "KEY" in name for name in carried)
    assert "environment_file" not in declared
    settings = overlay().brief_environment.settings
    assert [(s.env, s.secret) for s in settings] == [
        ("EXAMPLE_LINEAR_KEY", "EXAMPLE_LINEAR_KEY")
    ]


def _declared_paths(tmp_path: Path) -> tuple[Path, Path]:
    claude = tmp_path / "home" / ".config/onex/lane-claude"
    return claude / "settings.json", claude / "plugins/known_marketplaces.json"


def test_the_unit_declares_the_claude_directory_the_runner_reads_and_the_files_in_it() -> (
    None
):
    declared = load_units()[UNIT]
    assert (
        declared["environment"]["ONEX_HOST_DRIFT_OWNER_CLAUDE_DIR"]
        == "{home}/.config/onex/lane-claude"
    )
    paths = [entry["path"] for entry in declared["declared_files"]]
    assert paths == [
        "{home}/.config/onex/lane-claude/settings.json",
        "{home}/.config/onex/lane-claude/plugins/known_marketplaces.json",
    ]


def test_a_dry_run_lists_both_declared_files_with_the_registry_root_filled(
    tmp_path: Path,
) -> None:
    path = tmp_path / "branch"
    revision = checkout(path)
    result = HandlerRegistryUnits(Recorder()).handle(request(tmp_path, path, revision))
    assert result.status == "success", result.stderr
    settings, markets = _declared_paths(tmp_path)
    assert f"declared file {settings}:" in result.stdout
    assert f"declared file {markets}:" in result.stdout
    assert '"installLocation": "/example/marketplace"' in result.stdout
    assert f'"ONEX_HOST_DRIFT_OWNER_CLAUDE_DIR={settings.parent}"' in result.rendered
    assert not settings.exists()  # a dry run writes nothing


def test_an_install_writes_both_files_and_keeps_the_one_it_replaces(
    tmp_path: Path,
) -> None:
    path = tmp_path / "branch"
    revision = checkout(path)
    first = HandlerRegistryUnits(Recorder()).handle(
        request(tmp_path, path, revision, mode="install")
    )
    assert first.status == "success", first.stderr
    settings, markets = _declared_paths(tmp_path)
    assert json.loads(settings.read_text(encoding="utf-8")) == {}
    entry = json.loads(markets.read_text(encoding="utf-8"))["omninode-internal"]
    assert entry["installLocation"] == "/example/marketplace"
    assert oct(markets.stat().st_mode & 0o777) == "0o600"
    # the same install again replaces nothing; a changed marketplace root keeps the replaced file
    again = HandlerRegistryUnits(Recorder()).handle(
        request(tmp_path, path, revision, mode="install")
    )
    assert again.status == "success", again.stderr
    assert not markets.with_name(markets.name + ".previous").exists()
    moved = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            path,
            revision,
            mode="install",
            marketplace_root="/example/other-marketplace",
        )
    )
    assert moved.status == "success", moved.stderr
    kept = json.loads(
        markets.with_name(markets.name + ".previous").read_text(encoding="utf-8")
    )
    assert kept["omninode-internal"]["installLocation"] == "/example/marketplace"
    assert "/example/other-marketplace" in markets.read_text(encoding="utf-8")


def test_an_install_that_names_no_marketplace_root_is_refused_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_MARKETPLACE_ROOT", raising=False)
    path = tmp_path / "branch"
    revision = checkout(path)
    result = HandlerRegistryUnits(Recorder()).handle(
        request(tmp_path, path, revision, mode="install", marketplace_root="")
    )
    assert result.status == "failure"
    assert "{marketplace_root}" in result.stderr
    assert not _declared_paths(tmp_path)[1].exists()


def test_a_declared_path_outside_the_home_is_refused(tmp_path: Path) -> None:
    unit = dict(load_units()[UNIT])
    unit["declared_files"] = [{"path": "{home}/../outside/known.json", "document": {}}]
    handler = HandlerRegistryUnits(Recorder())
    with pytest.raises(ValueError, match="not a path under the home"):
        handler.declared_file_writes(request(tmp_path, tmp_path, "x"), unit)
    unit["declared_files"] = [{"path": "/etc/known.json", "document": {}}]
    with pytest.raises(ValueError, match="not a path under the home"):
        handler.declared_file_writes(request(tmp_path, tmp_path, "x"), unit)


def test_a_declared_document_with_an_unfilled_placeholder_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    unit = dict(load_units()[UNIT])
    unit["declared_files"] = [
        {"path": "{home}/lane-claude/known.json", "document": {"where": "{missing}"}}
    ]
    handler = HandlerRegistryUnits(Recorder())
    with pytest.raises(ValueError, match=r"\{missing\}"):
        handler.declared_file_writes(request(tmp_path, tmp_path, "x"), unit)
    # failing control: the same declaration with a placeholder the request fills is accepted
    unit["declared_files"][0]["document"] = {"where": "{registry_root}"}
    ((written, text, mode),) = handler.declared_file_writes(
        request(tmp_path, tmp_path, "x"), unit
    )
    assert written == tmp_path / "home" / "lane-claude/known.json"
    assert str(tmp_path / "omni") in text
    assert mode == 0o600


def test_a_launchd_unit_that_declares_files_is_refused() -> None:
    unit = dict(load_units()["factory-floor-publish"])
    unit["declared_files"] = [{"path": "{home}/x.json", "document": {}}]
    handler = HandlerRegistryUnits(Recorder())
    request_ = ModelRegistryUnitsRequest.model_validate(
        {
            "unit": "factory-floor-publish",
            "home": "/example/home",
            "registry_root": "/example/omni",
        }
    )
    with pytest.raises(ValueError, match="only systemd units may declare them"):
        handler.declared_file_writes(request_, unit)
