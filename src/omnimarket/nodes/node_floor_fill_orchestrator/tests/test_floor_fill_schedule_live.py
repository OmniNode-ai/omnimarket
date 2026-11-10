# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live profile of the fill's own tick unit: ``schedule install --profile live`` renders the live binding."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_fill_schedule import (
    Runner,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillStatus,
    load_floor_fill_schedule,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    ModelFloorFillLiveSettings,
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


@pytest.mark.parametrize("key", sorted(ModelFloorFillLiveSettings.model_fields))
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
    section = live_section(tmp_path)
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
