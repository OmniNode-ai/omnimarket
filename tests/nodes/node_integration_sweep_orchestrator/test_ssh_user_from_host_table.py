# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sweep's ssh login is the lab host row's ``ssh_user`` (OMN-22621).

No login lives in source: the target is ``<ssh_user>@<host>`` built from the row of the lab
host table, and a host with no row or a row with no ``ssh_user`` is refused, so the probe
reports ``error`` and runs no ssh.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_integration_sweep_orchestrator.handlers.surface_probes import (
    probe_container_health,
    probe_db_tables,
    probe_kafka_topics,
    ssh_destination,
)
from tests.node_overlay_support import (
    RUNTIME_HOST_ROW,
    install_lab_host_table,
)

_SUBPROCESS = (
    "omnimarket.nodes.node_integration_sweep_orchestrator."
    "handlers.surface_probes.subprocess.run"
)
_HOST = str(RUNTIME_HOST_ROW["target"])


@pytest.fixture(autouse=True)
def _no_ambient_table(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ONEX_LAB_RUN_HOSTS", "ONEX_SKILL_OVERLAY_ROOTS", "OMNI_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)


def _ok(stdout: str = "") -> MagicMock:
    result = MagicMock()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = ""
    return result


@pytest.mark.unit
def test_destination_is_the_rows_ssh_user_at_the_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_lab_host_table(
        monkeypatch, tmp_path, [{**RUNTIME_HOST_ROW, "ssh_user": "row-login"}]
    )
    assert ssh_destination(_HOST) == f"row-login@{_HOST}"


@pytest.mark.unit
def test_destination_matches_a_row_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_lab_host_table(monkeypatch, tmp_path, [RUNTIME_HOST_ROW])
    assert ssh_destination("h-example") == "lab-user@h-example"


@pytest.mark.unit
def test_container_health_ssh_argv_carries_the_rows_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_lab_host_table(
        monkeypatch, tmp_path, [{**RUNTIME_HOST_ROW, "ssh_user": "row-login"}]
    )
    run_mock = MagicMock(return_value=_ok("svc\tUp 1 hour\n"))
    with patch(_SUBPROCESS, run_mock):
        result = probe_container_health(_HOST)
    assert run_mock.call_args.args[0][:2] == ["ssh", f"row-login@{_HOST}"]
    assert result["status"] == "pass"


@pytest.mark.unit
def test_kafka_and_db_ssh_argv_carry_the_rows_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_lab_host_table(
        monkeypatch, tmp_path, [{**RUNTIME_HOST_ROW, "ssh_user": "row-login"}]
    )
    run_mock = MagicMock(return_value=_ok("NAME\nt1\n"))
    with patch(_SUBPROCESS, run_mock):
        probe_kafka_topics(_HOST, "redpanda", ["t1"], [])
        probe_db_tables(_HOST, "pg", "postgres", "db", ["t"])
    destinations = {call.args[0][1] for call in run_mock.call_args_list}
    assert run_mock.call_count >= 2
    assert destinations == {f"row-login@{_HOST}"}


@pytest.mark.unit
def test_a_row_without_ssh_user_is_refused_and_runs_no_ssh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = {k: v for k, v in RUNTIME_HOST_ROW.items() if k != "ssh_user"}
    install_lab_host_table(monkeypatch, tmp_path, [row])
    run_mock = MagicMock(return_value=_ok())
    with patch(_SUBPROCESS, run_mock):
        results = [
            probe_container_health(_HOST),
            probe_kafka_topics(_HOST, "redpanda", ["t1"], []),
            probe_db_tables(_HOST, "pg", "postgres", "db", ["t"]),
        ]
    run_mock.assert_not_called()
    for result in results:
        assert result["status"] == "error", result["surface"]
        assert "declares no ssh_user" in result["details"]["error"]


@pytest.mark.unit
def test_a_host_with_no_row_is_refused_and_runs_no_ssh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_lab_host_table(monkeypatch, tmp_path, [RUNTIME_HOST_ROW])
    run_mock = MagicMock(return_value=_ok())
    with patch(_SUBPROCESS, run_mock):
        result = probe_container_health("203.0.113.7")
    run_mock.assert_not_called()
    assert result["status"] == "error"
    assert "no row has target or name 203.0.113.7" in result["details"]["error"]


@pytest.mark.unit
def test_with_no_host_table_the_probe_reports_error_naming_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: with no table at all nothing is probed and nothing is green."""
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    run_mock = MagicMock(return_value=_ok())
    with patch(_SUBPROCESS, run_mock):
        result = probe_container_health(_HOST)
    run_mock.assert_not_called()
    assert result["status"] == "error"
    assert "lab_hosts" in result["details"]["error"]


@pytest.mark.unit
def test_no_login_literal_remains_in_the_node_source() -> None:
    node_root = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_integration_sweep_orchestrator"
    )
    literal = re.compile(r"\"[A-Za-z_][A-Za-z0-9_-]*@\{")
    offenders = [
        f"{path.name}:{number}"
        for path in node_root.rglob("*.py")
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if literal.search(line)
    ]
    assert offenders == []
