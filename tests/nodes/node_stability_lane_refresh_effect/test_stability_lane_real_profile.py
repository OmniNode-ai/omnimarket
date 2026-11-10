# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-host profile end to end: overlay file, pin file, a stub refresh script, the CLI (OMN-21613)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_stability_lane_refresh_effect.adapters import (
    FileAppliedPinStore,
    SubprocessRefreshExecutor,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.runtime_stability_lane_refresh import (
    main,
)
from tests.nodes.node_stability_lane_refresh_effect.stability_support import (
    PIN,
    PIN_TEXT,
)

pytestmark = pytest.mark.unit


def _git(clone: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def _host(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    for repo, tag in PIN.as_pairs():
        clone = home / repo
        clone.mkdir(parents=True)
        _git(clone, "init", "-q")
        _git(
            clone,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        )
        _git(clone, "tag", tag)
    pin = tmp_path / "pin.yaml"
    pin.write_text(PIN_TEXT)
    script = tmp_path / "refresh.sh"
    script.write_text(
        '#!/bin/sh\necho "$@" >> "$STUB_LOG"\necho "$LANE_PROBE_HOST $EXAMPLE_PORT" >> "$STUB_LOG"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    root = tmp_path / "overlays" / "node_stability_lane_refresh_effect"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "lane": "example-lane",
                "box_id": "example-box",
                "probe_host": "probe.invalid",
                "health_port": 18085,
                "expected_environment": "example-env",
                "ports": {"EXAMPLE_PORT": 15436},
                "workspace_config_root": str(tmp_path),
                "registry_root": str(home),
                "pin_file": str(pin),
                "refresh_script": str(script),
                "state_dir": str(tmp_path / "state"),
                "deploy_reason": "OMN-0: example",
                "refresh_timeout_seconds": 30,
                "health_timeout_seconds": 2,
            }
        )
    )
    return tmp_path / "overlays"


def test_cli_executes_once_then_recreates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    roots = _host(tmp_path)
    log = tmp_path / "stub.log"
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(roots))
    monkeypatch.setenv("STUB_LOG", str(log))
    assert main(["refresh", "--execute"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["outcome"] == "refreshed"
    assert log.read_text().splitlines()[0].endswith("--execute")
    assert "probe.invalid 15436" in log.read_text()
    calls = len(log.read_text().splitlines())
    assert main(["refresh", "--execute"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["outcome"] == "already-pinned"
    assert len(log.read_text().splitlines()) == calls


def test_cli_refuses_a_dev_tag_and_a_different_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(_host(tmp_path)))
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "stub.log"))
    assert (
        main(
            [
                "refresh",
                "--infra",
                "dev-20261030195247-1e3c83494587",
                "--core",
                "v2.3.4",
                "--market",
                "v3.4.5",
            ]
        )
        == 64
    )
    capsys.readouterr()
    assert (
        main(["refresh", "--infra", "v1.2.3", "--core", "v2.3.4", "--market", "v3.4.9"])
        == 64
    )
    assert json.loads(capsys.readouterr().out)["outcome"] == "refused-not-release-tag"
    assert not (tmp_path / "stub.log").exists()


def test_cli_without_an_overlay_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", "")
    assert main(["refresh"]) == 3


def test_runner_reports_a_timeout_as_124() -> None:
    outcome = SubprocessRefreshExecutor().run(["sleep", "5"], {}, 1)
    assert outcome.return_code == 124


def test_applied_store_round_trips(tmp_path: Path) -> None:
    store = FileAppliedPinStore(str(tmp_path / "s"))
    assert store.read() is None
    store.write(PIN)
    assert store.read() == PIN
    assert Path(store.render_pin_file("lane: x\n")).read_text() == "lane: x\n"
    assert os.path.isdir(tmp_path / "s")
