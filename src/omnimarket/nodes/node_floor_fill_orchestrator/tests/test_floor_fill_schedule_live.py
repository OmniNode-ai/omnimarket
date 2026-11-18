# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live profile of the fill's own tick unit: ``schedule install --profile live`` renders the live binding."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_fill_schedule import (
    Runner,
    render_systemd,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillStatus,
    FloorFillConfigError,
    load_floor_fill_config,
    load_floor_fill_schedule,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    ModelFloorFillLiveSettings,
    load_floor_fill_live_settings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator import (
    main as runtime_main,
)
from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_schedule import (
    run_schedule,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    OVERLAY,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.test_floor_fill_live_binding import (
    live_section,
)

pytestmark = pytest.mark.unit

HOSTS = [("fill-host", True), ("other-host", True), ("away-host", False)]
OMNI_ROOT = Path("/srv/example/registry")
FAILED = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED]


def _registry(tmp_path: Path) -> Path:
    """A registry root whose dispatch venv holds an interpreter file, so a real install passes its check."""
    root = tmp_path / "registry"
    python = root / ".onex-dispatch-venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    return root


def _ok(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(command), 0, "", "")


def _install_live(
    tmp_path: Path,
    section: dict[str, Any],
    *extra: str,
    run: Runner | None = None,
    omni_root: Path | None = None,
) -> tuple[int, str, str]:
    result = run_schedule(
        [
            "install",
            "--this-host",
            OVERLAY["host_id"],
            "--workspace-root",
            str(omni_root or _registry(tmp_path)),
            *extra,
        ],
        {"floor_fill": OVERLAY, "floor_fill_live": section},
        hosts=HOSTS,
        home=tmp_path,
        run=run or _ok,
    )
    return result.exit_code, result.stdout, result.stderr


def exec_words(rendered: str) -> list[str]:
    """The words of the unit's one ExecStart line, as systemd reads them (``%%`` and ``$$`` unescaped)."""
    lines = [line for line in rendered.splitlines() if line.startswith("ExecStart=")]
    assert len(lines) == 1
    text = lines[0].removeprefix("ExecStart=").replace("%%", "%").replace("$$", "$")
    return shlex.split(text)


def test_live_profile_unit_exec_carries_profile_and_live_section(
    tmp_path: Path,
) -> None:
    section = live_section(tmp_path)
    code, out, _ = _install_live(tmp_path, section, "--dry-run", "--profile", "live")
    assert code == 0
    words = exec_words(out)
    assert words[-5:] == [
        "--this-host",
        OVERLAY["host_id"],
        "--profile",
        "live",
        "tick",
    ]
    assert json.loads(words[4]) == {
        "floor_fill": OVERLAY,
        "floor_fill_live": section,
        "lab_hosts": [{"name": OVERLAY["host_id"], "available": True}],
    }


def test_live_profile_unit_words_are_accepted_by_the_runtime_module(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rendered words are accepted by the runtime module itself (its argv after ``-m <module>``)."""
    section = live_section(tmp_path)
    code, out, _ = _install_live(tmp_path, section, "--dry-run", "--profile", "live")
    assert code == 0
    arguments = exec_words(out)[3:]
    code = runtime_main([*arguments[:-1], "lines"])
    assert capsys.readouterr().out == (
        "UNKNOWN fill-state:store-dsn-unset:" + section["store_dsn_env"] + "\n"
    )
    assert code == FAILED
    assert runtime_main([*arguments[:-1], "lines"]) == code


def test_default_install_unit_is_unchanged_by_the_live_option(
    tmp_path: Path,
) -> None:
    section = live_section(tmp_path)
    _, default_out, _ = _install_live(tmp_path, section, "--dry-run")
    _, inmemory_out, _ = _install_live(
        tmp_path, section, "--dry-run", "--profile", "inmemory"
    )
    assert default_out == inmemory_out
    assert "--profile" not in default_out


@pytest.mark.parametrize(
    "key",
    sorted(
        name
        for name, field in ModelFloorFillLiveSettings.model_fields.items()
        if field.is_required()
    ),
)
def test_live_profile_missing_key_refuses_naming_it(key: str, tmp_path: Path) -> None:
    section = {
        name: value for name, value in live_section(tmp_path).items() if name != key
    }
    code, out, err = _install_live(tmp_path, section, "--profile", "live")
    assert code == 1
    assert out == ""
    assert f"floor_fill_live.{key}" in err
    assert not (tmp_path / ".config/systemd/user").exists()


def test_live_profile_unknown_key_refuses(tmp_path: Path) -> None:
    section = live_section(tmp_path, extra_key="not-a-setting")
    code, out, err = _install_live(tmp_path, section, "--profile", "live")
    assert code == 1
    assert out == ""
    assert "floor_fill_live.extra_key" in err
    assert not (tmp_path / ".config/systemd/user").exists()


def test_live_profile_installed_unit_equals_dry_run(tmp_path: Path) -> None:
    env_file = str(tmp_path / "floor-fill.env")
    section = live_section(tmp_path, store_dsn_env_file=env_file)
    calls: list[list[str]] = []

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return _ok(command)

    root = _registry(tmp_path)
    code, dry, _ = _install_live(
        tmp_path, section, "--dry-run", "--profile", "live", run=run, omni_root=root
    )
    assert code == 0
    assert calls == []
    code, _, _ = _install_live(
        tmp_path, section, "--profile", "live", run=run, omni_root=root
    )
    assert code == 0
    schedule = load_floor_fill_schedule()
    unit_dir = tmp_path / ".config/systemd/user"
    service = (unit_dir / f"{schedule.unit_name}.service").read_text(encoding="utf-8")
    timer = (unit_dir / f"{schedule.unit_name}.timer").read_text(encoding="utf-8")
    assert service + "\n" + timer + "\n" == dry
    assert [
        line for line in service.splitlines() if line.startswith("EnvironmentFile=")
    ] == [f"EnvironmentFile=-{env_file}"]


def _unit_lines(rendered: str, key: str) -> list[str]:
    return [line for line in rendered.splitlines() if line.startswith(f"{key}=")]


def test_live_profile_unit_reads_the_store_dsn_from_the_declared_environment_file(
    tmp_path: Path,
) -> None:
    """The unit carries the DSN by file path only: the overlay names the file, never the value."""
    env_file = str(tmp_path / "floor-fill.env")
    section = live_section(tmp_path, store_dsn_env_file=env_file)
    code, out, _ = _install_live(tmp_path, section, "--dry-run", "--profile", "live")
    assert code == 0
    assert _unit_lines(out, "EnvironmentFile") == [f"EnvironmentFile=-{env_file}"]


def test_live_profile_unit_without_the_environment_file_key_renders_no_line(
    tmp_path: Path,
) -> None:
    section = live_section(tmp_path)
    code, out, _ = _install_live(tmp_path, section, "--dry-run", "--profile", "live")
    assert code == 0
    assert _unit_lines(out, "EnvironmentFile") == []
    assert "store_dsn_env_file" not in out


BAD_ENVIRONMENT_FILES = [
    pytest.param("", id="empty"),
    pytest.param("relative/floor-fill.env", id="relative"),
    pytest.param("/srv/has space/floor-fill.env", id="space"),
    pytest.param("/srv/x.env\n", id="trailing-newline"),
    pytest.param("/srv/x.env\nExecStartPre=/bin/true", id="newline-and-directive"),
    pytest.param("/srv/%h/x.env", id="percent-specifier"),
    pytest.param('/srv/"x.env', id="double-quote"),
    pytest.param("/srv/'x.env", id="single-quote"),
    pytest.param("/srv/x\\.env", id="backslash"),
    pytest.param("/srv/x\t.env", id="tab"),
    pytest.param("/srv/x\r.env", id="carriage-return"),
    pytest.param("/srv/x\x00.env", id="nul"),
    pytest.param("/srv/x\x1b.env", id="escape"),
]


@pytest.mark.parametrize("bad_path", BAD_ENVIRONMENT_FILES)
def test_live_profile_refuses_an_environment_file_store_dsn_env_file_a_unit_line_cannot_carry(
    tmp_path: Path, bad_path: str
) -> None:
    """Without ``--dry-run``: exit 1, the key named, no unit directory, and no command run."""
    section = live_section(tmp_path, store_dsn_env_file=bad_path)
    calls: list[list[str]] = []

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return _ok(command)

    code, out, err = _install_live(tmp_path, section, "--profile", "live", run=run)
    assert code == 1
    assert out == ""
    assert "floor_fill_live.store_dsn_env_file" in err
    assert not (tmp_path / ".config/systemd/user").exists()
    assert calls == []


@pytest.mark.parametrize("bad_path", BAD_ENVIRONMENT_FILES)
def test_store_dsn_env_file_the_settings_model_refuses_a_bad_path(
    bad_path: str, tmp_path: Path
) -> None:
    """The field validator: install and the tick runtime both load the settings through it."""
    with pytest.raises(FloorFillConfigError) as refused:
        load_floor_fill_live_settings(
            live_section(tmp_path, store_dsn_env_file=bad_path)
        )
    assert "floor_fill_live.store_dsn_env_file" in refused.value.keys
    assert "floor_fill_live.store_dsn_env_file" in str(refused.value)
    assert "not an absolute path a unit line can carry unquoted" in str(refused.value)


@pytest.mark.parametrize("bad_path", BAD_ENVIRONMENT_FILES)
def test_store_dsn_env_file_the_renderer_refuses_a_bad_path_itself(
    bad_path: str, tmp_path: Path
) -> None:
    """The renderer re-checks the path: a settings object built without validation is still refused."""
    live = ModelFloorFillLiveSettings.model_construct(
        **live_section(tmp_path, store_dsn_env_file=bad_path)
    )
    with pytest.raises(
        ValueError, match=re.escape("floor_fill_live.store_dsn_env_file")
    ):
        render_systemd(
            load_floor_fill_schedule(),
            load_floor_fill_config(OVERLAY),
            omni_root=OMNI_ROOT,
            home=tmp_path,
            hosts=HOSTS,
            live=live,
        )


@pytest.mark.parametrize("bad_path", BAD_ENVIRONMENT_FILES)
def test_store_dsn_env_file_the_tick_runtime_refuses_a_bad_path(
    bad_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad path in the overlay JSON stops the tick naming the key, before it looks for the DSN."""
    overlay = {
        "floor_fill": OVERLAY,
        "floor_fill_live": live_section(tmp_path, store_dsn_env_file=bad_path),
        "lab_hosts": [{"name": OVERLAY["host_id"], "available": True}],
    }
    code = runtime_main(
        [
            "--overlay-json",
            json.dumps(overlay),
            "--this-host",
            OVERLAY["host_id"],
            "--profile",
            "live",
            "lines",
        ]
    )
    assert capsys.readouterr().out == (
        "UNKNOWN fill-state:overlay:floor_fill_live.store_dsn_env_file\n"
    )
    assert code == FAILED


def test_store_dsn_env_file_an_absolute_path_is_accepted(tmp_path: Path) -> None:
    """Positive control: the refusals above are not a validator that refuses everything."""
    settings = load_floor_fill_live_settings(
        live_section(tmp_path, store_dsn_env_file="/srv/example/secrets/x-1.env")
    )
    assert settings.store_dsn_env_file == "/srv/example/secrets/x-1.env"
