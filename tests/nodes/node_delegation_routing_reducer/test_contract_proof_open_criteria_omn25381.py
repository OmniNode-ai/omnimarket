# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local Done gate reads open_criteria; the AC1 run target is the named repository."""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from .test_contract_proof_omn24980 import (
    CAUSE_TEST,
    contract,
    recorded,
    run_ac1_check,
)

pytestmark = pytest.mark.unit

CONTRACTS = Path(__file__).parents[3] / "contracts"
OPEN = {
    "OMN-24980": ["AC1", "AC2", "AC3"],
    "OMN-25284": ["AC1", "AC5"],
    "OMN-25320": ["AC7"],
    "OMN-25381": ["AC1", "AC2", "AC5", "AC7"],
}


def gate_item(ticket: str) -> dict[str, Any]:
    data = yaml.safe_load((CONTRACTS / f"{ticket}.yaml").read_text())
    items = [i for i in data["dod_evidence"] if i["id"].endswith("-open-criteria-gate")]
    assert len(items) == 1, ticket
    return items[0]


def run_gate(
    ticket: str, data: dict[str, Any], cwd: Path
) -> subprocess.CompletedProcess[str]:
    (cwd / "contracts").mkdir(parents=True, exist_ok=True)
    (cwd / f"contracts/{ticket}.yaml").write_text(yaml.safe_dump(data))
    command = gate_item(ticket)["checks"][0]["check_value"]
    return subprocess.run(
        ["bash", "-c", command],
        check=False,
        text=True,
        capture_output=True,
        env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"},
        cwd=cwd,
    )


@pytest.mark.parametrize("ticket", sorted(OPEN))
def test_open_criteria_gate_is_a_local_done_gate_item_that_binds_nothing(
    ticket: str,
) -> None:
    item = gate_item(ticket)
    assert item["execution_scope"] == "local_done_gate"
    assert "binds_ac" not in item


@pytest.mark.parametrize("ticket", sorted(OPEN))
def test_open_criteria_gate_refuses_done_naming_every_open_criterion(
    ticket: str, tmp_path: Path
) -> None:
    data = yaml.safe_load((CONTRACTS / f"{ticket}.yaml").read_text())
    assert sorted(data["context"]["open_criteria"]) == OPEN[ticket]
    refused = run_gate(ticket, data, tmp_path)
    assert refused.returncode == 1, refused.stderr
    for label in OPEN[ticket]:
        assert f"open criterion {label}" in refused.stderr


def test_open_criteria_gate_fails_on_null_review_ticket_dev_run_record_owner_tickets(
    tmp_path: Path,
) -> None:
    # The owner tickets and the hosted dev run of OMN-24980 are null today: the
    # gate names each, and fills of each one by one still leave it refusing.
    data = contract()
    refused = run_gate("OMN-24980", data, tmp_path / "null")
    assert refused.returncode == 1
    assert "owner ticket dev_mounted_ladder is null" in refused.stderr
    assert "owner ticket local_share_alarm is null" in refused.stderr
    assert "no passing hosted dev run" in refused.stderr
    # Only the criteria closed: the null tickets and the null run still refuse.
    closed = copy.deepcopy(data)
    closed["context"]["open_criteria"] = {}
    still = run_gate("OMN-24980", closed, tmp_path / "criteria_only")
    assert still.returncode == 1
    assert "owner ticket" in still.stderr
    assert "no passing hosted dev run" in still.stderr
    # Positive control: everything met, the gate passes.
    met = copy.deepcopy(closed)
    for entry in met["context"]["owner_tickets"].values():
        entry["ticket"] = "OMN-25098"
    met["context"]["dev_ladder_run"]["passing_hosted_run"] = {"run_id": "x"}
    passed = run_gate("OMN-24980", met, tmp_path / "met")
    assert passed.returncode == 0, passed.stderr


def test_dod_omn25320_ac2_items_do_not_pass_for_open_criteria() -> None:
    # dod-omn25320-ac2 selects the open_criteria tests by keyword; this module's
    # gate tests carry that keyword, so the item fails if the gate stops refusing.
    data = yaml.safe_load((CONTRACTS / "OMN-25320.yaml").read_text())
    item = next(i for i in data["dod_evidence"] if i["id"] == "dod-omn25320-ac2")
    assert "open_criteria" in item["checks"][0]["check_value"]
    assert "binds_ac" not in gate_item("OMN-25320")


def test_contract_proof_ac1_runs_in_the_named_owning_repository_not_a_stand_in_name(
    tmp_path: Path,
) -> None:
    # The base and head run resolve ``$OMNI_HOME/<repo>`` from the recorded
    # cause_fix_test.repo: a repository named omnimarket (the owner of this
    # change) is the run target, and its recorded test is run at its base_ref and
    # at its head. The item that runs it is dod-omn24980-ac1-live, at the local
    # Done gate.
    data = contract()
    item = next(i for i in data["dod_evidence"] if i["id"] == "dod-omn24980-ac1-live")
    assert item["execution_scope"] == "local_done_gate"
    assert 'r="$OMNI_HOME/$1"' in item["checks"][0]["check_value"]
    served = run_ac1_check(
        tmp_path / "named",
        data,
        recorded(),
        test_body=CAUSE_TEST,
        repo_name="omnimarket",
    )
    assert served.returncode == 0, served.stderr
    assert "PASS" in served.stdout
    # The run target follows the recorded repo: a record naming a repository the
    # home does not hold is refused, not run against another.
    other = contract()
    other["context"]["criterion_status"]["AC1"]["cause_fix_test"] = {
        "repo": "omnibase_infra",
        "path": "tests/test_cause.py",
        "base_ref": "HEAD",
    }
    refused = run_ac1_check(
        tmp_path / "other",
        other,
        recorded(),
        test_body=CAUSE_TEST,
        repo_name="omnimarket",
    )
    assert refused.returncode != 0
    assert "does not exist" in refused.stderr


def test_open_criteria_no_hosted_check_of_omn25381_binds_an_unmet_criterion() -> None:
    # Review of c9f3605f3e04, finding 6: AC1 (full run, owner ruling) and AC2
    # (null owner tickets, null hosted run) were bound to hosted checks that pass
    # while both are unmet; AC5 likewise while the gate reads the declared table.
    data = yaml.safe_load((CONTRACTS / "OMN-25381.yaml").read_text())
    unmet = set(data["context"]["open_criteria"])
    declared = {
        entry["id"]
        for requirement in data["requirements"]
        for entry in requirement["acceptance"]
    }
    assert {"AC1", "AC2", "AC5"} <= unmet
    assert not unmet & declared
    for item in data["dod_evidence"]:
        assert not set(item.get("binds_ac") or ()) & unmet, item["id"]
