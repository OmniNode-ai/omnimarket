# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The server and worker runtimes of the Temporal host units (OMN-24900): the pin, the store path and the argv."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_temporal_health_alarm_effect import (
    runtime_temporal_health_alarm as alarm,
)
from omnimarket.nodes.node_temporal_health_alarm_effect import (
    runtime_temporal_server as server,
)
from omnimarket.nodes.node_temporal_health_alarm_effect import (
    runtime_temporal_worker as worker,
)

UNITS = (
    Path(server.__file__).parents[1]
    / "node_host_installer_registry_units"
    / "contract.yaml"
)


def _archive(body: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        info = tarfile.TarInfo("temporal")
        info.size = len(body)
        bundle.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _settings(tmp_path: Path, archive: bytes, sha: str) -> dict[str, object]:
    settings = dict(server.server_settings())
    settings["sha256"] = sha
    target = server.release_directory(settings, tmp_path)
    target.mkdir(parents=True)
    (target / str(settings["asset"])).write_bytes(archive)
    return settings


@pytest.mark.unit
def test_contract_pins_one_release_by_sha256() -> None:
    settings = server.server_settings()
    assert settings["release"] == "v1.10.1"
    assert len(str(settings["sha256"])) == 64
    assert server.release_url(settings).endswith(
        "/v1.10.1/temporal_cli_1.10.1_linux_amd64.tar.gz"
    )


@pytest.mark.unit
def test_binary_is_extracted_when_the_archive_matches_the_pin(tmp_path: Path) -> None:
    archive = _archive(b"#!/bin/sh\n")
    settings = _settings(tmp_path, archive, hashlib.sha256(archive).hexdigest())
    binary = server.ensure_binary(settings, tmp_path)
    assert binary.read_bytes() == b"#!/bin/sh\n"
    assert binary.stat().st_mode & 0o111


@pytest.mark.unit
def test_archive_that_differs_from_the_pin_is_refused(tmp_path: Path) -> None:
    archive = _archive(b"changed")
    settings = _settings(tmp_path, archive, "0" * 64)
    with pytest.raises(ValueError, match="is not the pinned"):
        server.ensure_binary(settings, tmp_path)
    assert not (server.release_directory(settings, tmp_path) / "temporal").exists()


@pytest.mark.unit
def test_server_store_is_under_the_durable_home_directory(tmp_path: Path) -> None:
    settings = server.server_settings()
    argv = server.server_argv(settings, tmp_path, tmp_path / "temporal")
    store = argv[argv.index("--db-filename") + 1]
    assert store == str(tmp_path / ".local/share/onex-temporal/temporal.db")
    assert argv[argv.index("--port") + 1] == "7233"


@pytest.mark.unit
def test_probes_accept_the_unit_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    unit_args = ["--omni-home", "/x", "--state-dir", "/y", "--probe"]
    assert server.main(unit_args) == 0
    assert alarm.main(unit_args) == 0
    assert '"probe": "ok"' in capsys.readouterr().out


@pytest.mark.unit
def test_worker_runs_the_workflow_node_worker_verb_in_the_named_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "src/omnimarket/nodes/node_prove_running_workflow").mkdir(parents=True)
    monkeypatch.delenv("ONEX_TEMPORAL_ADDRESS", raising=False)
    monkeypatch.setenv("ONEX_TEMPORAL_WORKER_PROJECT", str(tmp_path))
    settings = worker.worker_settings()
    argv = worker.worker_argv(settings, worker.worker_project(settings), "uv")
    assert argv[:5] == ["uv", "run", "--frozen", "--project", str(tmp_path)]
    assert argv[5:9] == [
        "python",
        "-m",
        "omnimarket.nodes.node_prove_running_workflow",
        "worker",
    ]
    assert argv[argv.index("--target-host") + 1] == "127.0.0.1:7233"


@pytest.mark.unit
def test_worker_refuses_a_checkout_without_the_workflow_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_TEMPORAL_WORKER_PROJECT", str(tmp_path))
    with pytest.raises(ValueError, match="is not a checkout carrying"):
        worker.worker_project(worker.worker_settings())


@pytest.mark.unit
def test_units_are_declared_with_their_entry_files() -> None:
    units = yaml.safe_load(UNITS.read_text(encoding="utf-8"))["units"]
    root = Path(server.__file__).parents[4]
    for name in (
        "temporal-server-systemd",
        "temporal-worker-systemd",
        "temporal-health-alarm-systemd",
    ):
        unit = units[name]
        assert unit["platform"] == "systemd"
        assert (root / unit["entry_file"]).is_file()
    assert (
        units["temporal-health-alarm-systemd"]["environment"][
            "ONEX_TEMPORAL_TASK_QUEUE"
        ]
        == "onex-prove-running"
    )
