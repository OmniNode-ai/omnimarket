# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The canonical clone sync unit installer: the tick reaches the lab alarm delivery and writes refusals to the journal."""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_clone_sync_units.handlers.handler_clone_sync_units import (
    HandlerCloneSyncUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_clone_sync_units.models.model_clone_sync_units import (
    ModelCloneSyncUnitsRequest,
)

pytestmark = pytest.mark.unit

UNITS = ("canonical-clone-sync", "canonical-clone-sync-systemd")


class Recorder:
    """A runner that records every command and answers success with a canned stdout per program."""

    def __init__(
        self, answers: dict[str, subprocess.CompletedProcess[str]] | None = None
    ) -> None:
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        key = " ".join(argv[:2])
        return self.answers.get(key, subprocess.CompletedProcess(list(argv), 0, "", ""))


def _request(
    tmp_path: Path, unit: str, mode: str = "dry-run"
) -> ModelCloneSyncUnitsRequest:
    root = tmp_path / "registry"
    engine = root / "omniclaude/plugins/onex/hooks/lib/canonical_clone_sync.py"
    engine.parent.mkdir(parents=True)
    engine.write_text("", encoding="utf-8")
    return ModelCloneSyncUnitsRequest(
        unit=unit,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        home=str(tmp_path / "home"),
        registry_root=str(root),
        ledger_path=str(tmp_path / "ledger.md"),
    )


@pytest.mark.parametrize("unit", UNITS)
def test_the_tick_reaches_the_lab_alarm_delivery_overlay_from_the_registry_root(
    tmp_path: Path, unit: str
) -> None:
    """The overlay is found through ONEX_SKILL_OVERLAY_ROOTS and its paths are relative to the registry root."""
    request = _request(tmp_path, unit)
    result = HandlerCloneSyncUnits(Recorder()).handle(request)
    assert result.exit_code == 0, result.stderr
    roots = f"{request.registry_root}/overlays/overlays"
    if unit.endswith("systemd"):
        service = result.rendered["onex-canonical-clone-sync.service"]
        assert f"ONEX_SKILL_OVERLAY_ROOTS={roots}" in service
        assert f"WorkingDirectory={request.registry_root}\n" in service
        assert f"WorkingDirectory={request.home}" not in service
    else:
        plist = plistlib.loads(
            result.rendered["ai.omninode.canonical-clone-sync.plist"].encode()
        )
        assert plist["EnvironmentVariables"]["ONEX_SKILL_OVERLAY_ROOTS"] == roots
        assert plist["WorkingDirectory"] == request.registry_root


def test_the_systemd_tick_writes_its_refusals_to_the_journal(tmp_path: Path) -> None:
    """The alarm outcome is read with `journalctl --user -u onex-canonical-clone-sync.service`."""
    result = HandlerCloneSyncUnits(Recorder()).handle(
        _request(tmp_path, "canonical-clone-sync-systemd")
    )
    service = result.rendered["onex-canonical-clone-sync.service"]
    assert "StandardOutput=journal\n" in service
    assert "StandardError=journal\n" in service
    assert "append:" not in service
    assert "SyslogIdentifier=onex-canonical-clone-sync\n" in service


@pytest.mark.parametrize("unit", UNITS)
def test_the_ledger_path_and_registry_root_are_declared_with_no_default(
    tmp_path: Path, unit: str
) -> None:
    result = HandlerCloneSyncUnits(Recorder()).handle(_request(tmp_path, unit))
    text = "".join(result.rendered.values())
    assert str(tmp_path / "ledger.md") in text
    assert f"{tmp_path}/registry/.onex_state" in text
    with pytest.raises(ValueError, match="ledger_path"):
        ModelCloneSyncUnitsRequest(
            unit="canonical-clone-sync",
            home="/h",
            registry_root="/o",  # type: ignore[call-arg]
        )


def test_a_dry_run_writes_nothing_and_runs_no_command(tmp_path: Path) -> None:
    runner = Recorder()
    result = HandlerCloneSyncUnits(runner).handle(
        _request(tmp_path, "canonical-clone-sync-systemd")
    )
    assert result.changed is False
    assert not (tmp_path / "home/.config/systemd/user").exists()
    assert all(call[0] != "systemctl" for call in runner.calls)


def test_a_systemd_install_writes_both_units_and_enables_the_timer(
    tmp_path: Path,
) -> None:
    runner = Recorder()
    result = HandlerCloneSyncUnits(runner).handle(
        _request(tmp_path, "canonical-clone-sync-systemd", "install")
    )
    assert result.exit_code == 0, result.stderr
    unit_dir = tmp_path / "home/.config/systemd/user"
    assert (unit_dir / "onex-canonical-clone-sync.service").is_file()
    assert (unit_dir / "onex-canonical-clone-sync.timer").is_file()
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-canonical-clone-sync.timer",
    ] in (runner.calls)


def test_an_install_is_refused_when_the_node_does_not_import_under_the_interpreter(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def failing(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        code = 1 if "-I" in argv else 0
        return subprocess.CompletedProcess(
            list(argv), code, "", "ModuleNotFoundError" * (code)
        )

    result = HandlerCloneSyncUnits(failing).handle(
        _request(tmp_path, "canonical-clone-sync-systemd", "install")
    )
    assert result.status == "refused"
    assert "does not import" in result.reason
    assert not (tmp_path / "home/.config/systemd/user").exists()


def test_an_absent_engine_is_refused(tmp_path: Path) -> None:
    request = _request(tmp_path, "canonical-clone-sync-systemd", "install")
    (
        Path(request.registry_root)
        / "omniclaude/plugins/onex/hooks/lib/canonical_clone_sync.py"
    ).unlink()
    result = HandlerCloneSyncUnits(Recorder()).handle(request)
    assert result.status == "refused"
    assert "absent" in result.reason


def test_a_systemd_status_reads_the_journal(tmp_path: Path) -> None:
    runner = Recorder(
        {
            "journalctl --user": subprocess.CompletedProcess(
                [], 0, "REFUSED clone alarm=sent\n", ""
            )
        }
    )
    result = HandlerCloneSyncUnits(runner).handle(
        _request(tmp_path, "canonical-clone-sync-systemd", "status")
    )
    assert "REFUSED clone alarm=sent" in result.stdout
    assert [
        "journalctl",
        "--user",
        "-u",
        "onex-canonical-clone-sync.service",
        "-n",
        "5",
        "--no-pager",
    ] in runner.calls


def test_both_declared_units_name_the_overlay_root() -> None:
    for name, unit in load_units().items():
        assert (
            unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"]
            == "{registry_root}/overlays/overlays"
        ), name
