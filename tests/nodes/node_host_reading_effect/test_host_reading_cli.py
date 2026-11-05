# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verbs end to end against a sandbox: real files, a real overlay root, no ledger written."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_reading_effect.runtime_host_reading import main
from tests.nodes.node_host_reading_effect.host_reading_support import (
    EXAMPLE,
    LEDGER_HOST,
    World,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    sandbox = World("filesystem", tmp_path)
    overlay_dir = tmp_path / "overlays/node_host_reading_effect"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "overlay.yaml").write_text(EXAMPLE.read_text(encoding="utf-8"))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.setenv("OMNI_HOME", sandbox.environ["OMNI_HOME"])
    monkeypatch.setenv("ONEX_LEDGER_PATH", sandbox.environ["ONEX_LEDGER_PATH"])
    monkeypatch.setenv("ONEX_LEDGER_HOST_NAME", LEDGER_HOST)
    return sandbox


def test_probe_reads_working_and_appends_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["probe"]) == 0
    out = capsys.readouterr().out
    assert out.count("verdict=WORKING") == 2
    assert out.count("ledger=not-written") == 2
    assert "RUN host=example-ledger-host probes=2 failed=0 write=no" in out


def test_probe_shows_the_named_breaks_and_a_not_working_reading_is_exit_zero(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.break_restore_old_name_as_regular_file()
    world.break_plant_stale_queued_request(age_seconds=10**9)
    assert main(["probe"]) == 0
    out = capsys.readouterr().out
    assert "probe=ledger-old-names verdict=NOT_WORKING" in out
    assert "reason=old-name-not-a-link-docs-old-ledger-md" in out
    assert "probe=ledger-outbox verdict=NOT_WORKING" in out
    assert "reason=oldest-over-900s" in out


def test_hourly_appends_through_the_configured_command(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = tmp_path / "rows.txt"
    command = tmp_path / "append"
    command.write_text(
        "#!/bin/sh\n"
        f'cat >> "{sink}"\n'
        'echo \'{"status": "accepted", "message": "ok"}\'\n'
    )
    command.chmod(0o755)
    monkeypatch.setenv("ONEX_LEDGER_BUS_APPEND_COMMAND", str(command))
    assert main(["hourly"]) == 0
    rows = sink.read_text().splitlines()
    assert len(rows) == 2
    assert "ledger=written" in capsys.readouterr().out
    assert all(" | STATUS | lane=host-reading | src=host-reading" in r for r in rows)


def test_append_that_does_not_land_is_exit_one(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = tmp_path / "append"
    command.write_text("#!/bin/sh\ncat >/dev/null\nexit 65\n")
    command.chmod(0o755)
    monkeypatch.setenv("ONEX_LEDGER_BUS_APPEND_COMMAND", str(command))
    assert main(["hourly"]) == 1
    assert "ledger=append-failed:refused" in capsys.readouterr().out


def test_undeclared_host_is_exit_one(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ONEX_LEDGER_HOST_NAME", "other")
    assert main(["probe"]) == 1
    assert "host-not-declared:other" in capsys.readouterr().err


def test_host_name_unset_is_indeterminate(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_HOST_NAME")
    assert main(["probe"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_overlay_missing_a_key_is_indeterminate(
    world: World,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "overlays/node_host_reading_effect/overlay.yaml"
    document = yaml.safe_load(path.read_text())
    del document["outbox"]["oldest_max_seconds"]
    path.write_text(yaml.safe_dump(document))
    assert main(["probe"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
