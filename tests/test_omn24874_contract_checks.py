# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The checks the OMN-24874 contract binds are real readings, not greps that also match a failure (OMN-24874 contract)."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "OMN-24874.yaml"
# The proxy items that bound AC2, AC3 and AC5 to tests that pass whether or not the AC holds.
_REMOVED_PROXIES = {
    "dod-omn24874-ac2-r9-node",
    "dod-omn24874-ac3-fix-in-tree",
    "dod-omn24874-ac5-reviewed-head",
}


def _items() -> dict[str, dict[str, object]]:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return {item["id"]: item for item in document["dod_evidence"]}


def _commands() -> list[str]:
    return [
        str(check["check_value"])
        for item in _items().values()
        for check in item["checks"]  # type: ignore[attr-defined]
    ]


@pytest.mark.unit
def test_omn24874_contract_has_no_grep_for_success() -> None:
    # `grep -q success` also matches {"success": false} and {"conclusion": "failure", "note": "no success"}.
    assert not [c for c in _commands() if "grep -q success" in c]


@pytest.mark.unit
def test_omn24874_contract_live_reading_parses_the_conclusion_field(
    tmp_path: Path,
) -> None:
    command = str(_items()["dod-omn24874-live-readings"]["checks"][0]["check_value"])  # type: ignore[index]

    def run(r8: dict[str, object], r9: dict[str, object]) -> int:
        for name, record in (("r8", r8), ("r9", r9)):
            path = (
                tmp_path / f".local/state/omni/delegation-scenario-{name}/last-run.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record), encoding="utf-8")
        return subprocess.run(
            shlex.split(command),
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
            check=False,
            capture_output=True,
        ).returncode

    ok = {"conclusion": "success", "ran_at": "2026-11-16T14:48:30+00:00"}
    assert run(ok, ok) == 0
    # The shapes a grep for the word success accepts, and a parse of the conclusion rejects.
    assert run({"conclusion": "failure", "success": True}, ok) != 0
    assert run(ok, {"conclusion": "refused", "refused": "no success route"}) != 0
    assert run(ok, {"success": False, "conclusion": "failure"}) != 0


@pytest.mark.unit
def test_omn24874_contract_binds_no_proxy_test_to_ac2_ac3_or_ac5() -> None:
    items = _items()
    assert not (_REMOVED_PROXIES & set(items))
    for item in items.values():
        bound = set(item.get("binds_ac") or [])  # type: ignore[call-overload]
        if bound & {"AC2", "AC3", "AC5"}:
            # Only a live reading (a command in the local Done gate) may bind these.
            assert item.get("execution_scope") == "local_done_gate", item["id"]
            assert all(
                c["check_type"] == "command"
                for c in item["checks"]  # type: ignore[attr-defined]
            ), item["id"]


@pytest.mark.unit
def test_omn24874_contract_real_postgres_file_is_a_dod_command() -> None:
    commands = [
        c for c in _commands() if "test_omn24874_tenant_overlay_cost_real_postgres" in c
    ]
    assert commands, "no dod command names the real-Postgres file"
    # A run that skips any of its tests must fail the command.
    assert all("grep -q skipped" in c for c in commands)
