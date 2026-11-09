# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_serve_units: the rendered serve environment, its refusals and its install sequence (OMN-23365)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omnimarket.models.overlay_keys import ModelLabHostsTable
from omnimarket.nodes.node_host_installer_serve_units.handlers.handler_serve_units import (
    NODE_DIR,
    HandlerServeUnits,
)
from omnimarket.nodes.node_host_installer_serve_units.models.model_serve_units import (
    ModelServeUnitsOverlay,
    ModelServeUnitsRequest,
)

ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
SERVE_ENV = "serve-a.env"
REFRESH_ENV = "refresh-a.env"
SERVE_UNIT = "unit-a.service"
REFRESH_UNIT = "unit-b.service"
KB_VARIABLE = "KB_CLONE_PATH"
CONFIG_ROOT_VARIABLE = "ONEX_WORKSPACE_CONFIG_ROOT"


def _overlay(roots: dict[str, list[str]]) -> ModelServeUnitsOverlay:
    return ModelServeUnitsOverlay.model_validate(
        {
            "serve": {
                "name": SERVE_UNIT,
                "ticket": "OMN-10001",
                "env_file": f".config/app/{SERVE_ENV}",
                "log_directory": ".local/state/app/serve-a",
                "import_probe": "pkg.serve_a",
            },
            "clone_refresh": {
                "name": REFRESH_UNIT,
                "ticket": "OMN-10002",
                "env_file": f".config/app/{REFRESH_ENV}",
                "log_directory": ".local/state/app/refresh-a",
                "import_probe": "pkg.refresh_a",
            },
            "lane_environment": {
                "SRC_ROOT": {"repo": "Acme/engine"},
                "VOCAB_FILE": {"repo": "Acme/config", "inside": "docs/vocab.yaml"},
                CONFIG_ROOT_VARIABLE: {"repo": "Acme/config"},
            },
            "knowledge_base": {"repo": "Acme/notes"},
            "knowledge_base_variable": KB_VARIABLE,
            "skill_overlay_roots": roots,
        }
    )


class Host:
    """A temporary pool host: registry root with the serve venv, the overlay roots and a recording runner."""

    def __init__(
        self, tmp_path: Path, *, linger: str = "yes", probe_rc: int = 0
    ) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.root = tmp_path / "registry"
        self.python = self.root / "omnimarket/.venv/bin/python"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("#!/bin/sh\n")
        self.python.chmod(0o755)
        for name in ("plugin-overlays", "private-overlays"):
            (self.root / name).mkdir()
        self.kb = tmp_path / "notes"
        (self.kb / ".git").mkdir(parents=True)
        self.commands: list[list[str]] = []
        self.linger, self.probe_rc, self.active = linger, probe_rc, "inactive\n"
        self.roots = {
            "pool-a": ["$OMNI_HOME/plugin-overlays", "$OMNI_HOME/private-overlays"]
        }

    def run(self, argv, cwd=None, unset=()):  # type: ignore[no-untyped-def]
        argv = list(argv)
        self.commands.append(argv)
        if argv[:2] == [str(self.python), "-c"]:
            return subprocess.CompletedProcess(argv, self.probe_rc, "", "")
        if argv[0] == "loginctl":
            return subprocess.CompletedProcess(argv, 0, f"{self.linger}\n", "")
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return subprocess.CompletedProcess(argv, 0, self.active, "")
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    def table(
        self, *, local: bool = False, bus_lane: str | None = "test-lane"
    ) -> ModelLabHostsTable:
        clones = [
            {"repo": "Acme/engine", "ref": "dev", "path": "$OMNI_HOME/engine"},
            {"repo": "Acme/config", "ref": "main", "path": "$OMNI_HOME/../config"},
            {"repo": "Acme/notes", "ref": "main", "path": str(self.kb)},
        ]
        row = {
            "name": "pool-a",
            "target": "pool-a",
            "unit_slots": 6,
            "penalty": 0.0,
            "local": local,
            "clones": clones,
        }
        return ModelLabHostsTable.model_validate({"hosts": [row], "bus_lane": bus_lane})

    def handler(self, **kw) -> HandlerServeUnits:  # type: ignore[no-untyped-def]
        return HandlerServeUnits(
            run=self.run,
            hosts=kw.pop("hosts", self.table),
            overlay=kw.pop("overlay", lambda: _overlay(self.roots)),
            user=lambda: "lab",
            **kw,
        )

    def request(self, **kw) -> ModelServeUnitsRequest:  # type: ignore[no-untyped-def]
        base = {
            "mode": "dry-run",
            "host_name": "pool-a",
            "serve_root": str(self.root),
            "registry_root": str(self.root),
            "home": str(self.home),
        }
        return ModelServeUnitsRequest(**{**base, **kw})


def _lines(text: str) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in text.splitlines()
        if "=" in line and not line.startswith("#")
    )


def test_serve_env_names_skill_overlay_roots_from_the_host_overlay(
    tmp_path: Path,
) -> None:
    """AC2: the serve environment carries ONEX_SKILL_OVERLAY_ROOTS from the node's overlay."""
    host = Host(tmp_path)
    result = host.handler().handle(host.request())
    assert result.status == "success", result.stderr
    serve = _lines(result.environments[SERVE_ENV])
    expected = os.pathsep.join(
        [f"{host.root}/plugin-overlays", f"{host.root}/private-overlays"]
    )
    assert serve[ROOTS_ENV] == expected
    assert ROOTS_ENV not in _lines(result.environments[REFRESH_ENV])


def test_serve_env_keeps_the_declared_serve_arguments_and_lane_variables(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    result = host.handler().handle(host.request())
    serve = _lines(result.environments[SERVE_ENV])
    assert serve["LAB_WORK_HOST_NAME"] == "pool-a"
    assert serve["LAB_WORK_BUS_LANE"] == "test-lane"
    assert serve["LAB_WORK_RANK_PENALTY"] == "0"
    assert serve["LAB_WORK_MAX_UNITS"] == "6"
    assert serve["SRC_ROOT"] == f"{host.root}/engine"
    assert serve[CONFIG_ROOT_VARIABLE] == f"{host.root}/../config"
    assert serve["VOCAB_FILE"] == f"{host.root}/../config/docs/vocab.yaml"
    assert serve["LAB_WORK_ROOT"] == f"{host.home}/lab-run"
    clone = _lines(result.environments[REFRESH_ENV])
    assert clone[KB_VARIABLE] == str(host.kb)
    assert clone["CLONE_REFRESH_HOST_NAME"] == "pool-a"
    assert clone["CLONE_REFRESH_SYNC_COMMAND"].startswith(
        f'"python3 {host.root}/omniclaude/plugins/onex/hooks/lib/canonical_clone_sync.py sync --repo'
    )
    assert not (host.home / ".config").exists()


def test_golden_chain_install_writes_units_and_starts_them_in_order(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    result = host.handler().handle(host.request(mode="install"))
    assert result.status == "success", result.stderr
    assert result.changed is True
    assert ROOTS_ENV in (host.home / ".config/app" / SERVE_ENV).read_text()
    for name, tmpl, env in (
        (SERVE_UNIT, "serve", SERVE_ENV),
        (REFRESH_UNIT, "clone-refresh", REFRESH_ENV),
    ):
        text = (host.home / ".config/systemd/user" / name).read_text()
        assert f"EnvironmentFile=%h/.config/app/{env}" in text
        assert f"SyslogIdentifier={name.removesuffix('.service')}" in text
        assert "__" not in text
        assert (NODE_DIR / "templates" / f"{tmpl}.service.tmpl").is_file()
    systemctl = [c[2:] for c in host.commands if c[:2] == ["systemctl", "--user"]]
    assert systemctl[0] == ["daemon-reload"]
    assert systemctl.index(["enable", SERVE_UNIT]) < systemctl.index(
        ["restart", SERVE_UNIT]
    )
    assert systemctl.index(["restart", SERVE_UNIT]) < systemctl.index(
        ["status", SERVE_UNIT, "--no-pager"]
    )


def test_second_install_changes_nothing_and_restarts_nothing(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.handler().handle(host.request(mode="install"))
    host.commands.clear()
    host.active = "active\n"
    again = host.handler().handle(host.request(mode="install"))
    assert again.status == "success"
    assert again.changed is False
    assert "no change" in again.stdout
    assert not [c for c in host.commands if "restart" in c or "daemon-reload" in c]


def test_uninstall_removes_what_install_wrote(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.handler().handle(host.request(mode="install"))
    result = host.handler().handle(host.request(mode="uninstall"))
    assert result.status == "success"
    assert not list((host.home / ".config/systemd/user").iterdir())
    assert not list((host.home / ".config/app").iterdir())


def test_request_roots_replace_the_overlay_roots(tmp_path: Path) -> None:
    host = Host(tmp_path)
    own = tmp_path / "own"
    own.mkdir()
    result = host.handler().handle(host.request(skill_overlay_roots=(str(own),)))
    assert _lines(result.environments[SERVE_ENV])[ROOTS_ENV] == str(own)


# --- error chain: each refusal names its reason and writes nothing ------------------------------------------------


def _refused(result, reason: str, code: int, host: Host) -> None:  # type: ignore[no-untyped-def]
    assert result.status == "refused", result.stderr
    assert (result.reason, result.exit_code) == (reason, code)
    assert not (host.home / ".config").exists()


def test_host_the_overlay_does_not_list_is_refused_not_defaulted(
    tmp_path: Path,
) -> None:
    """The failing control for AC2: with no roots declared for the host, the serve env is not rendered."""
    host = Host(tmp_path)
    host.roots = {"pool-b": ["$OMNI_HOME/plugin-overlays"]}
    result = host.handler().handle(host.request(mode="install"))
    _refused(result, "no-skill-overlay-roots", 66, host)
    assert not result.environments


def test_root_that_is_not_a_directory_is_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.handler().handle(
        host.request(skill_overlay_roots=(str(tmp_path / "absent"),))
    )
    _refused(result, "skill-overlay-root-missing", 66, host)


def test_undeclared_host_is_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.handler().handle(host.request(host_name="pool-z"))
    _refused(result, "unknown-host", 2, host)


def test_table_without_a_bus_lane_is_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.handler(hosts=lambda: host.table(bus_lane=None)).handle(
        host.request()
    )
    _refused(result, "no-bus-lane", 2, host)


def test_launching_host_is_refused_for_systemd(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.handler(hosts=lambda: host.table(local=True)).handle(host.request())
    _refused(result, "local-host", 64, host)


def test_missing_serve_interpreter_is_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.handler().handle(host.request(serve_root=str(tmp_path / "elsewhere")))
    _refused(result, "serve-python-missing", 66, host)


def test_serve_that_cannot_import_its_module_is_refused(tmp_path: Path) -> None:
    host = Host(tmp_path, probe_rc=1)
    result = host.handler().handle(host.request(mode="install"))
    _refused(result, "import-probe-failed", 66, host)


def test_knowledge_base_that_is_not_a_clone_is_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    result = host.handler().handle(host.request(knowledge_base_path=str(plain)))
    _refused(result, "knowledge-base-not-a-clone", 66, host)


def test_linger_off_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    host = Host(tmp_path, linger="no")
    result = host.handler().handle(host.request(mode="install"))
    _refused(result, "linger-off", 69, host)


def test_host_command_failure_ends_the_run_with_its_status(tmp_path: Path) -> None:
    host = Host(tmp_path)

    def failing(argv, cwd=None, unset=()):  # type: ignore[no-untyped-def]
        if list(argv)[:3] == ["systemctl", "--user", "daemon-reload"]:
            return subprocess.CompletedProcess(argv, 5, "", "boom")
        return host.run(argv, cwd, unset)

    result = HandlerServeUnits(
        run=failing,
        hosts=host.table,
        overlay=lambda: _overlay(host.roots),
        user=lambda: "lab",
    ).handle(host.request(mode="install"))
    assert result.status == "failure"
    assert result.exit_code == 5
    assert "boom" in result.stderr


@pytest.mark.parametrize("name", ["serve.service.tmpl", "clone-refresh.service.tmpl"])
def test_unit_templates_read_their_environment_file_from_the_overlay(name: str) -> None:
    text = (NODE_DIR / "templates" / name).read_text()
    assert "EnvironmentFile=%h/__ENV_FILE__" in text
    assert "SyslogIdentifier=__SYSLOG_IDENTIFIER__" in text
