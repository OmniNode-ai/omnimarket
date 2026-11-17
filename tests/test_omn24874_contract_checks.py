# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The checks the OMN-24874 contract binds are real readings, not greps that also match a failure (OMN-24874 contract)."""

from __future__ import annotations

import json
import re
import shlex
import stat
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.delegation_scenario_runtime import JournalScenarioEventSink
from omnimarket.models.model_delegation_scenario import EnumScenarioTrigger
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    InMemoryLane,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    HandlerScenarioR8,
    load_runtime,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "OMN-24874.yaml"
# The proxy items that bound AC2, AC3 and AC5 to tests that pass whether or not the AC holds.
_REMOVED_PROXIES = {
    "dod-omn24874-ac2-r9-node",
    "dod-omn24874-ac3-fix-in-tree",
    "dod-omn24874-ac5-reviewed-head",
}


def _items() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return {item["id"]: item for item in document["dod_evidence"]}


def _commands() -> list[str]:
    return [
        str(check["check_value"])
        for item in _items().values()
        for check in item["checks"]
    ]


@pytest.mark.unit
def test_omn24874_contract_has_no_grep_for_success() -> None:
    # `grep -q success` also matches {"success": false} and {"conclusion": "failure", "note": "no success"}.
    assert not [c for c in _commands() if "grep -q success" in c]


_PINNED_HOST = "hp-gen9"  # h053's hostname, the host every live reading of the contract is pinned to


def _hostname_shim(directory: Path, name: str) -> dict[str, str]:
    """An environment whose ``hostname`` prints ``name``, so a host pin can be shown to hold and to refuse."""
    shim = directory / "bin" / "hostname"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(f"#!/bin/sh\necho {name}\n", encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return {"PATH": f"{shim.parent}:/usr/bin:/bin:/usr/local/bin"}


# The R8 and R9 max ages the live readings carry. The alarm node's own contract declares the same numbers (its tests tie them to
# the units' start_interval); this file pins the numbers the contract commands use.
_MAX_AGES = {"R8": 27000, "R9": 4500}


def _max_age(scenario: str) -> int:
    return _MAX_AGES[scenario]


@pytest.mark.unit
def test_omn24874_contract_live_reading_parses_the_conclusion_field(
    tmp_path: Path,
) -> None:
    command = str(_items()["dod-omn24874-live-readings"]["checks"][0]["check_value"])

    def run(
        r8: Mapping[str, object], r9: Mapping[str, object], host: str = _PINNED_HOST
    ) -> int:
        for name, record in (("r8", r8), ("r9", r9)):
            path = (
                tmp_path / f".local/state/omni/delegation-scenario-{name}/last-run.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record), encoding="utf-8")
        return subprocess.run(
            shlex.split(command),
            env={"HOME": str(tmp_path), **_hostname_shim(tmp_path, host)},
            check=False,
            capture_output=True,
        ).returncode

    fresh = datetime.now(UTC).isoformat()
    ok = {"conclusion": "success", "ran_at": fresh}
    assert run(ok, ok) == 0
    # The shapes a grep for the word success accepts, and a parse of the conclusion rejects.
    assert run({"conclusion": "failure", "success": True, "ran_at": fresh}, ok) != 0
    assert (
        run(
            ok,
            {"conclusion": "refused", "refused": "no success route", "ran_at": fresh},
        )
        != 0
    )
    assert run(ok, {"success": False, "conclusion": "failure", "ran_at": fresh}) != 0
    # Host and age are pinned: another host, or a record older than the alarm contract's max age, fails.
    assert run(ok, ok, host="some-other-host") != 0
    stale_r8 = (datetime.now(UTC) - timedelta(seconds=_max_age("R8") + 60)).isoformat()
    stale_r9 = (datetime.now(UTC) - timedelta(seconds=_max_age("R9") + 60)).isoformat()
    assert run({"conclusion": "success", "ran_at": stale_r8}, ok) != 0
    assert run(ok, {"conclusion": "success", "ran_at": stale_r9}) != 0
    # Inside the age it passes: the pin is the contract's own number, not a number that always fails.
    inside = (datetime.now(UTC) - timedelta(seconds=_max_age("R9") - 600)).isoformat()
    assert run(ok, {"conclusion": "success", "ran_at": inside}) == 0


@pytest.mark.unit
def test_omn24874_contract_live_command_ages_equal_the_alarm_contract_max_ages() -> (
    None
):
    # A number in a command that drifts from the alarm contract's max age is a live reading with the wrong tolerance.
    items = _items()
    live = str(items["dod-omn24874-live-readings"]["checks"][0]["check_value"])
    assert f'"r8 {_max_age("R8")}"' in live
    assert f'"r9 {_max_age("R9")}"' in live
    r9 = str(items["dod-omn24874-ac2-r9-live"]["checks"][0]["check_value"])
    assert r9.rstrip("' ").endswith(f"ran_at {_max_age('R9')}")
    r8 = str(items["dod-omn24874-ac1-r8-live-13-of-13"]["checks"][0]["check_value"])
    assert r8.rstrip("' ").endswith(f"recorded_at {_max_age('R8')}")
    for command in (live, r9, r8):
        assert f'test "$(hostname -s)" = {_PINNED_HOST}' in command


def _write_real_r8_row(home: Path, *, run_cost_usd: Decimal) -> None:
    """One R8 run through the real R8 handler, its result journaled by the real journal sink (the writer of h053's events.jsonl)."""
    journal = home / ".onex_state/delegation-scenario-r8/events.jsonl"
    handler = HandlerScenarioR8(
        load_runtime(),
        home=home,
        environment={},
        appender=lambda _row: None,
        sink=JournalScenarioEventSink(journal),
        terminal_topic="onex.evt.omnimarket.delegation-scenario-result.v1",
        lane_factory=lambda: InMemoryLane(run_cost_usd=run_cost_usd),
        trigger=EnumScenarioTrigger.SCHEDULE,
        clock=lambda: datetime.now(UTC),
        sleep=lambda _seconds: None,
    )
    handler.run()


@pytest.mark.unit
def test_omn24874_contract_r8_reading_means_cost_above_zero_on_a_real_writer_row(
    tmp_path: Path,
) -> None:
    # +organic_run_has_values is ``tokens > 0 and cost_usd > 0`` (handler_scenario_r8.py): the 13 of 13 reading rests on it.
    command = str(
        _items()["dod-omn24874-ac1-r8-live-13-of-13"]["checks"][0]["check_value"]
    )
    env = {"OMNI_HOME": str(tmp_path), **_hostname_shim(tmp_path, _PINNED_HOST)}

    def reading() -> int:
        return subprocess.run(
            shlex.split(command), env=env, check=False, capture_output=True
        ).returncode

    _write_real_r8_row(tmp_path, run_cost_usd=Decimal("0.000145"))
    journal = tmp_path / ".onex_state/delegation-scenario-r8/events.jsonl"
    row = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])["payload"]
    assert row["conclusion"] == "success"
    assert len(row["assertions"]) == 13
    assert "+organic_run_has_values" in row["assertions"]
    assert reading() == 0
    # The live h053 row of 2026-11-16T18:06Z: the organic control cost 0, so the one assertion read red and the reading fails.
    journal.unlink()
    _write_real_r8_row(tmp_path, run_cost_usd=Decimal(0))
    zero = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])["payload"]
    assert zero["conclusion"] == "failure"
    assert "-organic_run_has_values" in zero["assertions"]
    assert reading() != 0


def _ac5_command_env(
    tmp_path: Path, rows: list[str]
) -> tuple[str, dict[str, str], Path]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run(
        [
            *git,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "head",
        ],
        check=True,
    )
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    script = tmp_path / "plugin/skills/lane-brief/scripts/lane_brief.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import sys\nfor row in "
        + repr(rows)
        + ":\n    print(row.replace('<HEAD>', "
        + repr(head)
        + "))\n",
        encoding="utf-8",
    )
    command = str(
        _items()["dod-omn24874-ac5-clean-verdict-row"]["checks"][0]["check_value"]
    )
    env = {
        "OMNI_PLUGIN_ROOT": str(tmp_path / "plugin"),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
    }
    return command, env, repo


@pytest.mark.unit
def test_omn24874_contract_ac5_check_needs_the_clean_row_to_name_the_head(
    tmp_path: Path,
) -> None:
    older = "2026-11-16T19:44:33Z | STATUS | lane=x | pr=omnimarket#6335 | head=32e9b16c909d | verdict=CLEAN"
    named = "2026-11-16T21:00:00Z | STATUS | lane=x | pr=omnimarket#6335 | head=<HEAD> | verdict=CLEAN"
    # The row of a head this checkout is not at (an older CLEAN row after a new push) passes nothing.
    command, env, repo = _ac5_command_env(tmp_path, [older])
    stale = subprocess.run(
        shlex.split(command), cwd=repo, env=env, check=False, capture_output=True
    )
    assert stale.returncode != 0
    # A CLEAN row that names the checkout's 12-character head passes.
    command, env, repo = _ac5_command_env(tmp_path / "second", [older, named])
    ok = subprocess.run(
        shlex.split(command), cwd=repo, env=env, check=False, capture_output=True
    )
    assert ok.returncode == 0, ok.stderr


@pytest.mark.unit
def test_omn24874_contract_ac3_check_reads_the_r8_unit_and_the_dev_runtime() -> None:
    item = _items()["dod-omn24874-ac3-running-revision"]
    assert item["binds_ac"] == ["AC3"]
    command = str(item["checks"][0]["check_value"])
    # Host pinned, the revision read is the R8 unit's installed revision (not the gate host's checkout HEAD), a dev-merge tag
    # must contain it, and the newest dev-merge tag must be at most a day old.
    assert f'test "$(hostname -s)" = {_PINNED_HOST}' in command
    assert "scenario-r8-systemd.installed.json" in command
    assert "merge-base --is-ancestor" in command
    assert re.search(r'tag --contains "\$R" --list "dev-\*"', command)
    assert "86400" in command
    assert not re.search(r"is-ancestor \"\$F\" HEAD", command)
    # The squash merge method is read (repository settings), so the commit looked up is a non-merge commit on the first-parent
    # line of origin/dev and cannot be an unsquashed branch commit.
    assert "--first-parent --no-merges" in command
    # The dev runtime is read too: over ssh, in the runtime container, importing the handler that only the fix gives the
    # declared-price function; either variable unset fails the command instead of skipping the reading.
    assert "ssh -o BatchMode=yes" in command
    assert "${ONEX_RUNTIME_SSH_HOST:?" in command
    assert "${RUNTIME_CONTAINER:?" in command
    assert "handler_delegation_workflow" in command
    assert "m.byok_declared_price_per_1m" in command


@pytest.mark.unit
def test_omn24874_contract_ac3_dev_runtime_symbol_exists_only_with_the_fix() -> None:
    # The name the dev-runtime reading asserts is imported by the workflow handler of this change.
    from omnimarket.nodes.node_delegation_orchestrator.handlers import (
        handler_delegation_workflow as workflow,
    )

    assert callable(workflow.byok_declared_price_per_1m)


@pytest.mark.unit
def test_omn24874_contract_binds_no_proxy_test_to_ac2_ac3_or_ac5() -> None:
    items = _items()
    assert not (_REMOVED_PROXIES & set(items))
    for item in items.values():
        bound = set(item.get("binds_ac") or [])
        if bound & {"AC2", "AC3", "AC5"}:
            # Only a live reading (a command in the local Done gate) may bind these.
            assert item.get("execution_scope") == "local_done_gate", item["id"]
            assert all(c["check_type"] == "command" for c in item["checks"]), item["id"]


@pytest.mark.unit
def test_omn24874_contract_real_postgres_file_is_a_dod_command() -> None:
    commands = [
        c for c in _commands() if "test_omn24874_tenant_overlay_cost_real_postgres" in c
    ]
    assert commands, "no dod command names the real-Postgres file"
    # A run that skips any of its tests must fail the command.
    assert all("grep -q skipped" in c for c in commands)
