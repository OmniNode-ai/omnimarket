# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host bring-up run effect node.

The golden and error chains are in test_golden_chain_host_bringup_run_effect.py. Reading that
can fail: a dry run changes nothing, shown failing by the named injected break
``dry_run_runs_install``.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.nodes.node_host_bringup_run_effect import __main__ as cli
from omnimarket.nodes.node_host_bringup_run_effect.handlers import (
    handler_host_bringup_run as handler_module,
)
from omnimarket.nodes.node_host_bringup_run_effect.handlers.adapter_installer import (
    CliHostInstaller,
    FileDeployStore,
    InMemoryDeployStore,
    InMemoryInstaller,
    InMemoryPublisher,
)
from omnimarket.nodes.node_host_bringup_run_effect.handlers.handler_host_bringup_run import (
    HandlerHostBringupRun,
)
from omnimarket.nodes.node_host_bringup_run_effect.handlers.overlay import load_overlay
from omnimarket.nodes.node_host_bringup_run_effect.models import (
    EnumBringupMode,
    EnumStepOutcome,
    ModelHostBringupRunOverlay,
    ModelHostBringupRunRequest,
    ModelHostBringupRunResult,
    ModelInstallerOutcome,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(handler_module.__file__).resolve().parents[1]
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))

OVERLAY_DOC: dict[str, object] = {
    "installer_command": ["installer"],
    "repo_root": "/clone",
    "step_timeout_seconds": 30,
    "roles": {
        "linux-lab": {
            "steps": [
                {
                    "unit": "unit-a",
                    "host_name": True,
                    "rollback": "none",
                    "proof": "read a",
                },
                {
                    "unit": "unit-b",
                    "rollback": "uninstall",
                    "preview": "none",
                    "proof": "read b",
                },
                {
                    "unit": "unit-c",
                    "host_name": True,
                    "env": ["UNIT_C_PATH"],
                    "rollback": "uninstall",
                    "proof": "read c",
                },
                {
                    "unit": "unit-d",
                    "host_name": True,
                    "needs_deploy_store": True,
                    "rollback": "none",
                    "proof": "read d",
                },
            ]
        }
    },
}


def overlay() -> ModelHostBringupRunOverlay:
    return ModelHostBringupRunOverlay.model_validate(OVERLAY_DOC)


def run(
    mode: EnumBringupMode,
    *,
    installer: InMemoryInstaller | None = None,
    stores: frozenset[str] = frozenset({"h-new"}),
    environ: dict[str, str] | None = None,
    role: str = "linux-lab",
) -> tuple[ModelHostBringupRunResult, InMemoryInstaller]:
    installer = installer or InMemoryInstaller()
    handler = HandlerHostBringupRun(
        overlay=overlay(),
        installer=installer,
        deploy_store=InMemoryDeployStore(stores),
        publisher=InMemoryPublisher(),
        environ={"UNIT_C_PATH": "x"} if environ is None else environ,
    )
    result = handler.handle(
        ModelHostBringupRunRequest(host_name="h-new", mode=mode, role=role)
    )
    return result, installer


def assert_dry_run_changes_nothing(
    result: ModelHostBringupRunResult, installer: InMemoryInstaller
) -> None:
    """The reading: no installer ran in install or uninstall mode and the host is unchanged."""
    modes = {mode for _unit, mode, _host in installer.calls}
    assert modes <= {"dry-run"}, f"a dry run invoked {sorted(modes)}"
    assert result.changed_host is False


def test_dry_run_changes_nothing_and_does_not_run_a_unit_without_dry_run() -> None:
    result, installer = run(EnumBringupMode.DRY_RUN)
    assert_dry_run_changes_nothing(result, installer)
    by_unit = {step.unit: step for step in result.steps}
    assert by_unit["unit-b"].outcome is EnumStepOutcome.NOT_PREVIEWABLE
    assert "unit-b" not in [call[0] for call in installer.calls]
    assert result.completed


def test_failing_control_dry_run_that_installs_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected break dry_run_runs_install: the dry-run mode maps to install; the reading fails."""
    monkeypatch.setitem(
        handler_module._INSTALLER_MODE,
        EnumBringupMode.DRY_RUN,
        "install",
    )
    result, installer = run(EnumBringupMode.DRY_RUN)
    with pytest.raises(AssertionError, match="a dry run invoked"):
        assert_dry_run_changes_nothing(result, installer)


def test_missing_deploy_store_skips_the_step_in_every_mode() -> None:
    result, installer = run(EnumBringupMode.INSTALL, stores=frozenset())
    assert result.steps[-1].outcome is EnumStepOutcome.SKIPPED_NO_DEPLOY_STORE
    assert "unit-d" not in [call[0] for call in installer.calls]
    assert result.completed


def test_missing_env_refuses_before_the_installer_and_stops() -> None:
    result, installer = run(EnumBringupMode.INSTALL, environ={})
    assert (result.exit_code, result.stopped_at) == (64, 3)
    assert result.steps[-1].outcome is EnumStepOutcome.REFUSED_ENV
    assert "UNIT_C_PATH" in result.steps[-1].detail
    assert [call[0] for call in installer.calls] == ["unit-a", "unit-b"]


def test_rollback_runs_only_uninstall_steps_last_first() -> None:
    result, installer = run(EnumBringupMode.ROLLBACK)
    assert installer.calls == [
        ("unit-c", "uninstall", "h-new"),
        ("unit-b", "uninstall", None),
    ]
    skipped = {
        s.unit for s in result.steps if s.outcome is EnumStepOutcome.NOT_ROLLED_BACK
    }
    assert skipped == {"unit-a", "unit-d"}
    assert result.changed_host is True


def test_status_mode_asks_each_unit_for_its_status() -> None:
    result, installer = run(EnumBringupMode.STATUS)
    assert {mode for _u, mode, _h in installer.calls} == {"status"}
    assert result.changed_host is False


def test_unknown_role_is_refused_naming_the_declared_roles() -> None:
    result, installer = run(EnumBringupMode.DRY_RUN, role="no-such-role")
    assert result.exit_code == 1
    assert installer.calls == []
    assert result.refusal is not None
    assert "linux-lab" in result.refusal


def test_overlay_rejects_a_repeated_unit_and_a_bad_env_name() -> None:
    doc = yaml.safe_load(yaml.safe_dump(OVERLAY_DOC))
    doc["roles"]["linux-lab"]["steps"].append(
        dict(doc["roles"]["linux-lab"]["steps"][0])
    )
    with pytest.raises(ValueError, match="appears twice"):
        ModelHostBringupRunOverlay.model_validate(doc)
    doc = yaml.safe_load(yaml.safe_dump(OVERLAY_DOC))
    doc["roles"]["linux-lab"]["steps"][2]["env"] = ["lower"]
    with pytest.raises(ValueError, match="upper-case"):
        ModelHostBringupRunOverlay.model_validate(doc)


def test_overlay_loads_from_the_root_and_refuses_when_absent_or_broken(
    tmp_path: Path,
) -> None:
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    with pytest.raises(NodeOverlayNotFoundError, match="node_host_bringup_run_effect"):
        load_overlay(environ)
    node_dir = tmp_path / "node_host_bringup_run_effect"
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(OVERLAY_DOC), encoding="utf-8"
    )
    assert load_overlay(environ).repo_root == "/clone"
    (node_dir / "overlay.yaml").write_text("roles: {}\nextra: 1\n", encoding="utf-8")
    with pytest.raises(NodeOverlayError):
        load_overlay(environ)


def test_cli_installer_argv_carries_mode_flag_and_host_name() -> None:
    installer = CliHostInstaller(("uv", "run", "inst"), "/clone", 5)
    assert installer.argv("u", "install", None) == [
        "uv",
        "run",
        "inst",
        "u",
        "--repo-root",
        "/clone",
    ]
    assert installer.argv("u", "dry-run", "h") == [
        "uv", "run", "inst", "u", "--repo-root", "/clone", "--dry-run", "--host-name", "h",
    ]  # fmt: skip


def stub_installer(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "installer"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_real_backend_runs_the_command_and_reports_exit_timeout_and_missing(
    tmp_path: Path,
) -> None:
    log = tmp_path / "argv.log"
    ok = stub_installer(tmp_path, f'echo "$@" >> {log}\necho fine\nexit 0\n')
    out = CliHostInstaller((str(ok),), "/clone", 5).run("u", "status", "h")
    assert (out.exit_code, out.stdout.strip()) == (0, "fine")
    assert log.read_text().strip() == "u --repo-root /clone --status --host-name h"
    bad = stub_installer(tmp_path, "echo broke >&2\nexit 3\n")
    out = CliHostInstaller((str(bad),), "/clone", 5).run("u", "install", None)
    assert (out.exit_code, out.stderr.strip()) == (3, "broke")
    slow = stub_installer(tmp_path, "sleep 5\n")
    assert (
        CliHostInstaller((str(slow),), "/clone", 1).run("u", "install", None).exit_code
        == 124
    )
    missing = CliHostInstaller((str(tmp_path / "absent"),), "/clone", 1)
    assert missing.run("u", "install", None).exit_code == 127


def test_file_deploy_store_reads_the_declared_stores(tmp_path: Path) -> None:
    doc = tmp_path / "root.yaml"
    doc.write_text(
        yaml.safe_dump(
            {
                "hosts": {
                    "has": {"deploy_agent_env_stores": ["a.env"]},
                    "none": {"root": "x"},
                }
            }
        ),
        encoding="utf-8",
    )
    store = FileDeployStore(doc)
    assert store.has_deploy_agent_store("has") is True
    assert store.has_deploy_agent_store("none") is False
    assert store.has_deploy_agent_store("absent") is False
    assert FileDeployStore(None).has_deploy_agent_store("has") is False


def test_cli_main_dry_run_end_to_end_with_the_real_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "argv.log"
    script = stub_installer(tmp_path, f'echo "$@" >> {log}\nexit 0\n')
    doc = yaml.safe_load(yaml.safe_dump(OVERLAY_DOC))
    doc["installer_command"] = [str(script)]
    node_dir = tmp_path / "roots" / "node_host_bringup_run_effect"
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "roots"))
    monkeypatch.setenv("UNIT_C_PATH", "x")
    assert cli.main(["--host-name", "h-new"]) == 0
    printed = capsys.readouterr().out
    assert "BRING-UP dry-run host=h-new role=linux-lab steps=4" in printed
    assert "unit-b - not_previewable" in printed
    assert "changed_host=False" in printed
    lines = log.read_text().splitlines()
    assert lines
    assert all("--dry-run" in line for line in lines)
    assert not any(line.startswith("unit-b ") for line in lines)


def test_cli_main_without_an_overlay_refuses_naming_how_to_supply_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert cli.main(["--host-name", "h-new"]) == 66
    assert "ONEX_SKILL_OVERLAY_ROOTS" in capsys.readouterr().err


def test_step_detail_is_the_installer_line_not_a_tool_warning_on_a_pass() -> None:
    class Noisy:
        def run(
            self, unit: str, mode: str, host_name: str | None
        ) -> ModelInstallerOutcome:
            return ModelInstallerOutcome(
                exit_code=0, stdout="unit current\n", stderr="warning: tool noise\n"
            )

    result, _ = run(EnumBringupMode.STATUS, installer=Noisy())  # type: ignore[arg-type]
    assert result.steps[0].detail == "unit current"
