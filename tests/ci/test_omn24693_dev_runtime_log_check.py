# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev runtime log read of the host table keys ingest contract runs, and no contract holds an echo-only check.

The local Done gate item of contracts/OMN-24693.yaml reads the dev runtime log over ssh. These tests run its real
command with a stand-in ssh that prints fixture log text, so the gate's logic is exercised without the host.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / "contracts"
ITEM_ID = "dod-omn24693-ac3-dev-log-read"
WRITE_LINE = (
    "2026-11-16T04:10:00Z INFO omnimarket.nodes.node_host_table_keys_ingest_effect.handlers."
    "handler_host_table_keys_ingest: node_host_table_keys_ingest_effect wrote key=synthetic:hosts:v1:table "
    "digest=abc hosts=3 admitted=2"
)
IGNORED_LINE = "2026-11-16T04:10:01Z DEBUG overlay other_overlay ignored by node_host_table_keys_ingest_effect"
APPLIER_LINE = (
    "2026-11-16T04:10:02Z ERROR Auto-wiring boundary has no result applier for output-model "
    "ModelHostTableKeysIngestOutput"
)
OTHER_LINE = "2026-11-16T04:10:03Z INFO runtime heartbeat"
ECHO_ONLY = re.compile(r"^\s*(echo|printf|true|:)(\s|$)")
SHELL_CONTROL = re.compile(r"&&|\|\||[|;]|\$\(|`")


def _commands(contract: dict[str, object]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    items = contract.get("dod_evidence") or []
    assert isinstance(items, list)
    for item in items:
        for check in item.get("checks") or []:
            if check.get("check_type") == "command":
                found.append((item["id"], str(check["check_value"])))
    return found


def _is_echo_only(command: str) -> bool:
    """A command that only prints: it starts with echo and chains nothing that could fail."""
    # Single-quoted text is inert to the shell, so a ";" inside it chains nothing.
    unquoted = re.sub(r"'[^']*'", "''", command)
    return bool(ECHO_ONLY.match(command)) and not SHELL_CONTROL.search(unquoted)


def _check_command() -> str:
    contract = yaml.safe_load((CONTRACTS / "OMN-24693.yaml").read_text())
    matches = [cmd for item_id, cmd in _commands(contract) if item_id == ITEM_ID]
    assert len(matches) == 1
    return matches[0]


def _run(
    tmp_path: Path,
    log_text: str,
    *,
    since: str | None = "2026-11-16T04:00:00Z",
    ssh_fails: bool = False,
) -> subprocess.CompletedProcess[str]:
    fixture = tmp_path / "runtime.log"
    fixture.write_text(log_text)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    ssh = bin_dir / "ssh"
    body = "exit 255" if ssh_fails else f'cat "{fixture}"'
    ssh.write_text(f"#!/bin/sh\n# stand-in for ssh: prints the fixture log\n{body}\n")
    ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
    if since is not None:
        env["RECREATE_SINCE"] = since
    return subprocess.run(
        ["/bin/bash", "-c", _check_command()],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_a_clean_log_with_a_write_line_passes(tmp_path: Path) -> None:
    result = _run(tmp_path, "\n".join([OTHER_LINE, WRITE_LINE, OTHER_LINE]) + "\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no-result-applier=0" in result.stdout
    assert "ingest-write-lines=1" in result.stdout


def test_a_log_holding_a_no_result_applier_line_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, "\n".join([WRITE_LINE, APPLIER_LINE]) + "\n")
    assert result.returncode != 0
    assert "no-result-applier=1" in result.stdout


def test_a_log_with_no_write_line_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, "\n".join([OTHER_LINE, IGNORED_LINE]) + "\n")
    assert result.returncode != 0
    assert "ingest-write-lines=0" in result.stdout


def test_an_empty_log_fails(tmp_path: Path) -> None:
    assert _run(tmp_path, "").returncode != 0


def test_an_unset_recreate_time_fails_before_any_read(tmp_path: Path) -> None:
    result = _run(tmp_path, WRITE_LINE + "\n", since=None)
    assert result.returncode != 0
    assert "RECREATE_SINCE" in result.stderr


def test_a_failed_log_read_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, WRITE_LINE + "\n", ssh_fails=True)
    assert result.returncode != 0
    assert "log read failed" in result.stdout


def test_the_echo_only_detector_flags_an_echo_and_passes_a_real_check() -> None:
    assert _is_echo_only("echo 'pending a dev roll'")
    assert _is_echo_only("echo 'pending; read later | later'")
    assert _is_echo_only("true")
    assert not _is_echo_only(_check_command())
    assert not _is_echo_only("echo hi && test -s file")


def test_no_contract_holds_an_echo_only_check() -> None:
    offenders: list[str] = []
    for path in sorted(CONTRACTS.glob("*.yaml")):
        contract = yaml.safe_load(path.read_text())
        if not isinstance(contract, dict):
            continue
        offenders.extend(
            f"{path.name}:{item_id}: {command}"
            for item_id, command in _commands(contract)
            if _is_echo_only(command)
        )
    assert not offenders, "\n".join(offenders)
