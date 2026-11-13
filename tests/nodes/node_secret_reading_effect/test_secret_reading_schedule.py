# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The secret reading's hourly schedule: units from the contract, install, rollback (OMN-22659)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_secret_reading_effect.handlers.schedule import (
    SecretReadingScheduler,
    load_schedule,
    render_units,
)
from omnimarket.nodes.node_secret_reading_effect.overlay import (
    NODE_NAME,
    OVERLAY_ID_ENV,
    ModelSecretReadingOverlay,
    ModelSecretReadingScheduleHost,
    load_overlay,
    overlay_id,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_secret_reading_effect/overlay.example.yaml"
)


class _Recorder:
    """A runner that records argv and answers success, so no systemctl ever runs."""

    def __init__(self, failing: str | None = None) -> None:
        self.commands: list[list[str]] = []
        self.failing = failing

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        failed = bool(self.failing and self.failing in command)
        return subprocess.CompletedProcess(
            list(command), 1 if failed else 0, "", "boom" if failed else ""
        )


def _overlay() -> ModelSecretReadingOverlay:
    """The shipped example overlay, declaring one test host over a relative venv."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    base = ModelSecretReadingOverlay.model_validate(document)
    return base.model_copy(
        update={
            "schedule_hosts": (
                ModelSecretReadingScheduleHost(
                    name="h-test", scheduler="systemd-user", venv="venv"
                ),
            )
        }
    )


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "root"
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    return root, home, tmp_path / "overlays"


def _scheduler(home: Path, runner: _Recorder) -> SecretReadingScheduler:
    return SecretReadingScheduler(
        schedule=load_schedule(), overlay=_overlay(), home=home, run=runner
    )


def test_the_schedule_comes_from_the_contract_with_the_hourly_cadence() -> None:
    schedule = load_schedule()
    assert schedule.unit_name == "onex-secret-reading-hourly"
    assert schedule.cadence.on_calendar == "*-*-* *:05:00"


def test_rendered_units_run_the_node_read_with_the_overlays_route_facts(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    service, timer = render_units(
        load_schedule(),
        _overlay(),
        omni_root=root,
        venv=root / "venv",
        home=home,
        overlay_root=overlays,
    )
    execstart = next(
        line for line in service.splitlines() if line.startswith("ExecStart=")
    )
    assert execstart.endswith("-m omnimarket.nodes.node_secret_reading_effect read")
    assert f'Environment="OMNI_HOME={root}"' in service
    assert f'Environment="ONEX_SKILL_OVERLAY_ROOTS={overlays}"' in service
    assert 'Environment="ONEX_LEDGER_HOST_NAME=example-host"' in service
    assert "OnCalendar=*-*-* *:05:00" in timer
    assert "Persistent=true" in timer
    assert "Unit=onex-secret-reading-hourly.service" in timer


def test_install_for_a_host_the_overlay_does_not_declare_is_refused(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    runner = _Recorder()
    result = _scheduler(home, runner).install(
        "elsewhere", omni_root=root, overlay_root=overlays
    )
    assert result.exit_code == 1
    assert "not in the node_secret_reading_effect overlay" in result.stderr
    assert runner.commands == []
    assert not (home / ".config").exists()


def test_install_writes_both_units_under_the_user_directory_and_enables_the_timer(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    runner = _Recorder()
    result = _scheduler(home, runner).install(
        "h-test", omni_root=root, overlay_root=overlays
    )
    assert result.exit_code == 0, result.stderr
    unit_dir = home / ".config/systemd/user"
    assert (unit_dir / "onex-secret-reading-hourly.service").is_file()
    assert (unit_dir / "onex-secret-reading-hourly.timer").is_file()
    reload_at = runner.commands.index(["systemctl", "--user", "daemon-reload"])
    enable_at = runner.commands.index(
        ["systemctl", "--user", "enable", "--now", "onex-secret-reading-hourly.timer"]
    )
    assert reload_at < enable_at


def test_a_second_install_with_identical_text_reports_both_files_unchanged(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    scheduler = _scheduler(home, _Recorder())
    first = scheduler.install("h-test", omni_root=root, overlay_root=overlays)
    assert first.exit_code == 0, first.stderr
    again = scheduler.install("h-test", omni_root=root, overlay_root=overlays)
    assert again.exit_code == 0, again.stderr
    assert again.stdout.count("unchanged") == 2


def test_uninstall_disables_the_timer_and_removes_both_unit_files(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    _scheduler(home, _Recorder()).install(
        "h-test", omni_root=root, overlay_root=overlays
    )
    runner = _Recorder()
    result = _scheduler(home, runner).uninstall("h-test")
    assert result.exit_code == 0, result.stderr
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "onex-secret-reading-hourly.timer",
    ] in runner.commands
    assert list((home / ".config/systemd/user").iterdir()) == []


def test_a_dry_run_writes_nothing_and_returns_both_unit_texts(tmp_path: Path) -> None:
    root, home, overlays = _layout(tmp_path)
    runner = _Recorder()
    result = _scheduler(home, runner).install(
        "h-test", omni_root=root, overlay_root=overlays, dry_run=True
    )
    assert result.exit_code == 0, result.stderr
    assert "[Service]" in result.stdout
    assert "[Timer]" in result.stdout
    assert runner.commands == []
    assert not (home / ".config").exists()


def test_install_refuses_when_the_venv_cannot_import_the_node(tmp_path: Path) -> None:
    root, home, overlays = _layout(tmp_path)
    runner = _Recorder(failing="-c")
    result = _scheduler(home, runner).install(
        "h-test", omni_root=root, overlay_root=overlays
    )
    assert result.exit_code == 1
    assert "would not start" in result.stderr
    assert not (home / ".config").exists()


def _drill_overlay(drill_root: Path) -> ModelSecretReadingOverlay:
    return _overlay().model_copy(update={"drill_overlay_root": str(drill_root)})


def _drill_layout(tmp_path: Path) -> Path:
    drill_root = tmp_path / "overlays-drill"
    (drill_root / "node_secret_reading_effect").mkdir(parents=True)
    (drill_root / "node_secret_reading_effect" / "overlay.yaml").write_text(
        "drill: true\n", encoding="utf-8"
    )
    return drill_root


def test_the_contract_declares_a_daily_drill_unit() -> None:
    drill = load_schedule().drill
    assert drill is not None
    assert drill.unit_name == "onex-secret-reading-drill-daily"
    assert drill.cadence.on_calendar == "*-*-* 06:20:00"


def test_install_with_a_drill_root_writes_the_drill_pair_and_enables_both_timers(
    tmp_path: Path,
) -> None:
    root, home, overlay_root = _layout(tmp_path)
    drill_root = _drill_layout(tmp_path)
    runner = _Recorder()
    scheduler = SecretReadingScheduler(
        schedule=load_schedule(),
        overlay=_drill_overlay(drill_root),
        home=home,
        run=runner,
    )
    result = scheduler.install("h-test", omni_root=root, overlay_root=overlay_root)
    assert result.exit_code == 0, result.stderr
    units = home / ".config/systemd/user"
    drill_service = (units / "onex-secret-reading-drill-daily.service").read_text(
        encoding="utf-8"
    )
    assert f"ONEX_SKILL_OVERLAY_ROOTS={drill_root}" in drill_service
    assert "secret-reading-drill.out.log" in drill_service
    assert drill_service.rstrip().endswith(
        "SyslogIdentifier=onex-secret-reading-drill-daily"
    )
    drill_timer = (units / "onex-secret-reading-drill-daily.timer").read_text(
        encoding="utf-8"
    )
    assert "OnCalendar=*-*-* 06:20:00" in drill_timer
    assert "Unit=onex-secret-reading-drill-daily.service" in drill_timer
    hourly = (units / "onex-secret-reading-hourly.service").read_text(encoding="utf-8")
    assert f"ONEX_SKILL_OVERLAY_ROOTS={overlay_root}" in hourly
    enabled = [
        c[-1]
        for c in runner.commands
        if c[:4] == ["systemctl", "--user", "enable", "--now"]
    ]
    assert enabled == [
        "onex-secret-reading-hourly.timer",
        "onex-secret-reading-drill-daily.timer",
    ]


def test_install_refuses_a_drill_root_without_its_overlay_and_writes_nothing(
    tmp_path: Path,
) -> None:
    root, home, overlay_root = _layout(tmp_path)
    runner = _Recorder()
    scheduler = SecretReadingScheduler(
        schedule=load_schedule(),
        overlay=_drill_overlay(tmp_path / "missing"),
        home=home,
        run=runner,
    )
    result = scheduler.install("h-test", omni_root=root, overlay_root=overlay_root)
    assert result.exit_code == 1
    assert "no drill overlay" in result.stderr
    assert not (home / ".config/systemd/user").exists()
    assert runner.commands == []


def test_uninstall_removes_the_drill_pair_too(tmp_path: Path) -> None:
    root, home, overlay_root = _layout(tmp_path)
    drill_root = _drill_layout(tmp_path)
    runner = _Recorder()
    scheduler = SecretReadingScheduler(
        schedule=load_schedule(),
        overlay=_drill_overlay(drill_root),
        home=home,
        run=runner,
    )
    assert (
        scheduler.install("h-test", omni_root=root, overlay_root=overlay_root).exit_code
        == 0
    )
    runner.commands.clear()
    result = scheduler.uninstall("h-test")
    assert result.exit_code == 0
    assert list((home / ".config/systemd/user").iterdir()) == []
    disabled = [
        c[-1]
        for c in runner.commands
        if c[:4] == ["systemctl", "--user", "disable", "--now"]
    ]
    assert disabled == [
        "onex-secret-reading-hourly.timer",
        "onex-secret-reading-drill-daily.timer",
    ]


def test_run_now_with_drill_starts_the_drill_service(tmp_path: Path) -> None:
    _, home, _ = _layout(tmp_path)
    runner = _Recorder()
    scheduler = SecretReadingScheduler(
        schedule=load_schedule(),
        overlay=_drill_overlay(_drill_layout(tmp_path)),
        home=home,
        run=runner,
    )
    assert scheduler.run_now("h-test", drill=True).exit_code == 0
    assert runner.commands == [
        ["systemctl", "--user", "start", "onex-secret-reading-drill-daily.service"]
    ]


# --- the J4 join unit (OMN-23085) -------------------------------------------------------------------------------------------


def _j4_overlay_root(tmp_path: Path) -> Path:
    """An overlay root that holds the J4 join overlay, so the join's unit pair is installed."""
    root = tmp_path / "overlays-with-j4"
    (root / "node_secret_reading_join_j4").mkdir(parents=True)
    (root / "node_secret_reading_join_j4" / "overlay.yaml").write_text(
        "consumer: gate\n", encoding="utf-8"
    )
    return root


def test_the_contract_declares_an_hourly_j4_join_unit_after_the_secret_reading() -> (
    None
):
    join_j4 = load_schedule().join_j4
    assert join_j4 is not None
    assert join_j4.unit_name == "onex-secret-join-j4-hourly"
    assert join_j4.cadence.on_calendar == "*-*-* *:20:00"


def test_j4_unit_runs_the_join_cli_with_the_run_verb(tmp_path: Path) -> None:
    root, home, _ = _layout(tmp_path)
    service, timer = render_units(
        load_schedule(),
        _overlay(),
        omni_root=root,
        venv=root / "venv",
        home=home,
        overlay_root=tmp_path / "overlays",
        join_j4=True,
    )
    assert (
        "-m omnimarket.nodes.node_secret_reading_effect.runtime_join_j4 run\n"
        in service
    )
    assert "secret-join-j4.out.log" in service
    assert "SyslogIdentifier=onex-secret-join-j4-hourly" in service
    assert "OnCalendar=*-*-* *:20:00" in timer
    assert "Unit=onex-secret-join-j4-hourly.service" in timer


def test_install_with_the_j4_overlay_writes_the_j4_pair_and_enables_its_timer(
    tmp_path: Path,
) -> None:
    root, home, _ = _layout(tmp_path)
    overlay_root = _j4_overlay_root(tmp_path)
    runner = _Recorder()
    result = _scheduler(home, runner).install(
        "h-test", omni_root=root, overlay_root=overlay_root
    )
    assert result.exit_code == 0, result.stderr
    units = home / ".config/systemd/user"
    service = (units / "onex-secret-join-j4-hourly.service").read_text(encoding="utf-8")
    assert f"ONEX_SKILL_OVERLAY_ROOTS={overlay_root}" in service
    assert (units / "onex-secret-join-j4-hourly.timer").is_file()
    enabled = [
        c[-1]
        for c in runner.commands
        if c[:4] == ["systemctl", "--user", "enable", "--now"]
    ]
    assert enabled == [
        "onex-secret-reading-hourly.timer",
        "onex-secret-join-j4-hourly.timer",
    ]


def test_install_without_the_j4_overlay_installs_no_j4_unit(tmp_path: Path) -> None:
    root, home, overlays = _layout(tmp_path)
    runner = _Recorder()
    result = _scheduler(home, runner).install(
        "h-test", omni_root=root, overlay_root=overlays
    )
    assert result.exit_code == 0, result.stderr
    assert not (home / ".config/systemd/user/onex-secret-join-j4-hourly.timer").exists()
    assert "onex-secret-join-j4-hourly.timer" not in [c[-1] for c in runner.commands]


def test_uninstall_removes_the_j4_pair_too(tmp_path: Path) -> None:
    root, home, _ = _layout(tmp_path)
    runner = _Recorder()
    scheduler = _scheduler(home, runner)
    scheduler.install("h-test", omni_root=root, overlay_root=_j4_overlay_root(tmp_path))
    assert scheduler.uninstall("h-test").exit_code == 0
    units = home / ".config/systemd/user"
    assert not (units / "onex-secret-join-j4-hourly.service").exists()
    assert not (units / "onex-secret-join-j4-hourly.timer").exists()
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "onex-secret-join-j4-hourly.timer",
    ] in runner.commands


def test_run_now_with_join_j4_starts_the_j4_service(tmp_path: Path) -> None:
    _, home, _ = _layout(tmp_path)
    runner = _Recorder()
    assert _scheduler(home, runner).run_now("h-test", join_j4=True).exit_code == 0
    assert runner.commands == [
        ["systemctl", "--user", "start", "onex-secret-join-j4-hourly.service"]
    ]


def test_run_now_refuses_the_drill_and_the_j4_unit_together(tmp_path: Path) -> None:
    _, home, _ = _layout(tmp_path)
    runner = _Recorder()
    result = _scheduler(home, runner).run_now("h-test", drill=True, join_j4=True)
    assert result.exit_code == 1
    assert runner.commands == []


# --- the overlay id a unit pins (OMN-23713) ---------------------------------------------------------------------------------


def test_overlay_id_env_names_the_document_or_falls_back_to_the_node_name() -> None:
    assert overlay_id({}) == NODE_NAME
    assert (
        overlay_id({OVERLAY_ID_ENV: "  node_secret_reading_effect_htest  "})
        == "node_secret_reading_effect_htest"
    )
    assert overlay_id({OVERLAY_ID_ENV: "   "}) == NODE_NAME


def test_load_overlay_reads_the_document_the_overlay_id_env_names(
    tmp_path: Path,
) -> None:
    root = tmp_path / "overlays"
    document = root / "node_secret_reading_effect_htest" / "overlay.yaml"
    document.parent.mkdir(parents=True)
    document.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    overlay = load_overlay(
        environ={
            "ONEX_SKILL_OVERLAY_ROOTS": str(root),
            OVERLAY_ID_ENV: "node_secret_reading_effect_htest",
        }
    )
    assert overlay == ModelSecretReadingOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


def test_rendered_units_pin_the_overlay_id_env_only_when_it_is_not_the_nodes_own(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    pinned, _ = render_units(
        load_schedule(),
        _overlay(),
        omni_root=root,
        venv=root / "venv",
        home=home,
        overlay_root=overlays,
        overlay_id="node_secret_reading_effect_htest",
    )
    assert f'Environment="{OVERLAY_ID_ENV}=node_secret_reading_effect_htest"' in pinned
    own, _ = render_units(
        load_schedule(),
        _overlay(),
        omni_root=root,
        venv=root / "venv",
        home=home,
        overlay_root=overlays,
    )
    assert OVERLAY_ID_ENV not in own


@pytest.mark.parametrize("identifier", ["node-secret-reading", "Node_Secret_Reading"])
def test_render_units_refuse_an_overlay_id_that_is_not_a_plain_identifier(
    tmp_path: Path, identifier: str
) -> None:
    root, home, overlays = _layout(tmp_path)
    with pytest.raises(ValueError, match="is not a plain identifier"):
        render_units(
            load_schedule(),
            _overlay(),
            omni_root=root,
            venv=root / "venv",
            home=home,
            overlay_root=overlays,
            overlay_id=identifier,
        )


def _drill_id_overlay() -> ModelSecretReadingOverlay:
    """The example overlay with the drill named by id: a clone under the same root."""
    return _overlay().model_copy(
        update={"drill_overlay_id": "node_secret_reading_effect_htest_drill"}
    )


def _drill_id_document(overlay_root: Path) -> Path:
    """The drill's overlay document, cloned under the same root the hourly unit reads."""
    document = overlay_root / "node_secret_reading_effect_htest_drill" / "overlay.yaml"
    document.parent.mkdir(parents=True)
    document.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return document


def test_install_with_a_drill_overlay_id_writes_the_drill_pair_from_the_same_root(
    tmp_path: Path,
) -> None:
    root, home, overlay_root = _layout(tmp_path)
    _drill_id_document(overlay_root)
    runner = _Recorder()
    scheduler = SecretReadingScheduler(
        schedule=load_schedule(),
        overlay=_drill_id_overlay(),
        home=home,
        run=runner,
    )
    result = scheduler.install(
        "h-test",
        omni_root=root,
        overlay_root=overlay_root,
        overlay_id="node_secret_reading_effect_htest",
    )
    assert result.exit_code == 0, result.stderr
    units = home / ".config/systemd/user"
    hourly = (units / "onex-secret-reading-hourly.service").read_text(encoding="utf-8")
    assert f"ONEX_SKILL_OVERLAY_ROOTS={overlay_root}" in hourly
    assert f"{OVERLAY_ID_ENV}=node_secret_reading_effect_htest" in hourly
    drill_service = (units / "onex-secret-reading-drill-daily.service").read_text(
        encoding="utf-8"
    )
    assert f"ONEX_SKILL_OVERLAY_ROOTS={overlay_root}" in drill_service
    assert f"{OVERLAY_ID_ENV}=node_secret_reading_effect_htest_drill" in drill_service
    enabled = [
        c[-1]
        for c in runner.commands
        if c[:4] == ["systemctl", "--user", "enable", "--now"]
    ]
    assert enabled == [
        "onex-secret-reading-hourly.timer",
        "onex-secret-reading-drill-daily.timer",
    ]


def test_install_refuses_a_drill_overlay_id_without_its_overlay_and_writes_nothing(
    tmp_path: Path,
) -> None:
    root, home, overlay_root = _layout(tmp_path)
    runner = _Recorder()
    scheduler = SecretReadingScheduler(
        schedule=load_schedule(),
        overlay=_drill_id_overlay(),
        home=home,
        run=runner,
    )
    result = scheduler.install(
        "h-test",
        omni_root=root,
        overlay_root=overlay_root,
        overlay_id="node_secret_reading_effect_htest",
    )
    assert result.exit_code == 1
    assert "no drill overlay at" in result.stderr
    assert not (home / ".config").exists()
    assert runner.commands == []


def test_the_overlay_refuses_a_drill_overlay_root_together_with_a_drill_overlay_id() -> (
    None
):
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="both an overlay root and an overlay id"):
        ModelSecretReadingOverlay.model_validate(
            {**document, "drill_overlay_root": "x", "drill_overlay_id": "y"}
        )


def test_schedule_install_verb_pins_the_overlay_root_and_overlay_id_it_loaded_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The verb finds the document by the id the process is told, so the unit it renders reads the same one."""
    from omnimarket.nodes.node_secret_reading_effect.runtime_secret_reading import (
        main,
    )

    root = tmp_path / "overlays"
    document = root / "node_secret_reading_effect_htest" / "overlay.yaml"
    document.parent.mkdir(parents=True)
    document.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    monkeypatch.setenv(OVERLAY_ID_ENV, "node_secret_reading_effect_htest")
    code = main(
        [
            "schedule",
            "install",
            "--host",
            "example-host",
            "--workspace-root",
            str(tmp_path / "omni"),
            "--dry-run",
        ]
    )
    rendered = capsys.readouterr().out
    assert code == 0
    assert f'Environment="ONEX_SKILL_OVERLAY_ROOTS={root}"' in rendered
    assert (
        f'Environment="{OVERLAY_ID_ENV}=node_secret_reading_effect_htest"' in rendered
    )
