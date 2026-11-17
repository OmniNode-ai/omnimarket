# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the contract's receipt assertion against a recorded CLI receipt and its variants.

The receipt is the stdout of a real ``onex delegate --json`` run on the dev lane
(``fixtures/delegate_stdout_local.json``, run e97b77ba-c7d1-44db-94e9-4c807c215da3,
read 2026-11-17, host paths, addresses and the bus host redacted). It is the
runtime envelope that ``omnibase_infra.cli.cli_delegate`` prints, not the
``runs/<run_id>/receipt.json`` file, so it carries no top-level ``bus``, ``lane``,
``locus``, ``backend_id`` or ``routing_tier`` key.
"""

from __future__ import annotations

import copy
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.cli.delegate_terminal_resolver import resolve_delegate_terminal
from omnibase_infra.cli.model_receipt_runtime_summary import ModelReceiptRuntimeSummary

pytestmark = pytest.mark.unit
CONTRACT = Path(__file__).parents[3] / "contracts/OMN-24980.yaml"
RECORDED = Path(__file__).parent / "fixtures/delegate_stdout_local.json"
INTEGRATION_PROOF = "dod-omn24980-integration-proof"


def contract() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT.read_text())
    assert isinstance(loaded, dict)
    return loaded


def proof_command() -> str:
    item = next(i for i in contract()["dod_evidence"] if i["id"] == INTEGRATION_PROOF)
    return str(item["checks"][0]["check_value"])


def validator() -> str:
    argv = shlex.split(proof_command())
    candidates = [
        argv[i + 2]
        for i, word in enumerate(argv[:-2])
        if word == "python3" and argv[i + 1] == "-c"
    ]
    assert candidates, "proof must parse the answering receipt, not grep candidates"
    return candidates[-1]


def recorded() -> dict[str, Any]:
    loaded = json.loads(RECORDED.read_text())
    assert isinstance(loaded, dict)
    return loaded


def payload(receipt: dict[str, Any]) -> dict[str, Any]:
    inner = receipt["result"]["terminal_payload"]["payload"]
    assert isinstance(inner, dict)
    return inner


def check(tmp_path: Path, receipt: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))
    return subprocess.run(
        [sys.executable, "-c", validator(), str(path)],
        check=False,
        text=True,
        capture_output=True,
    )


def test_recorded_receipt_matches_the_omnibase_infra_schema() -> None:
    receipt = recorded()
    summary = ModelReceiptRuntimeSummary.model_validate(receipt["result"])
    assert summary.handler_locus == "dispatched"
    terminal = resolve_delegate_terminal(summary.terminal_payload)
    assert terminal.status == "completed"
    assert terminal.accepted_attempt is not None
    assert receipt["status"] == "success"
    # Not the runs/<run_id>/receipt.json file shape.
    assert not {"bus", "lane", "locus", "backend_id", "routing_tier"} & set(receipt)


def test_proof_accepts_the_recorded_dev_runtime_answer(tmp_path: Path) -> None:
    completed = check(tmp_path, recorded())
    assert completed.returncode == 0, completed.stderr
    command = shlex.split(proof_command())
    assert command[command.index("--bus") + 1] == "kafka"
    assert command[command.index("--lane") + 1] == "dev"
    assert command[command.index("--locus") + 1] == "deployed-lane"
    assert not any(
        word.startswith("DELEGATION_ROUTING_TIERS_PATH=") for word in command
    )


def mutate(name: str) -> dict[str, Any]:
    receipt = copy.deepcopy(recorded())
    inner = payload(receipt)
    attempt = inner["attempts"][0]
    if name == "in_process":
        receipt["result"]["handler_locus"] = "in_process"
    elif name == "top_level_failed":
        receipt["status"] = "failed"
    elif name == "exit_code_nonzero":
        receipt["exit_code"] = 1
    elif name == "workflow_not_completed":
        receipt["result"]["workflow_result"] = "timeout"
    elif name == "terminal_not_completed_under_success":
        assert receipt["status"] == "success"
        inner["status"] = "failed"
    elif name == "terminal_missing":
        receipt["result"]["terminal_payload"] = None
    elif name == "hosted_answer":
        attempt["backend_id"] = "hosted-answer"
        attempt["tier"] = "cheap_cloud"
    elif name == "quality_gate_failed":
        attempt["quality_gate_passed"] = False
    elif name == "skipped_local_candidate":
        skipped = copy.deepcopy(attempt)
        skipped["acceptance_decision"] = "skip"
        skipped["quality_gate_passed"] = False
        attempt["backend_id"] = "hosted-answer"
        attempt["tier"] = "cheap_cloud"
        inner["attempts"] = [skipped, attempt]
    else:
        raise AssertionError(name)
    return receipt


@pytest.mark.parametrize(
    "name",
    [
        "in_process",
        "top_level_failed",
        "exit_code_nonzero",
        "workflow_not_completed",
        "terminal_not_completed_under_success",
        "terminal_missing",
        "hosted_answer",
        "quality_gate_failed",
        "skipped_local_candidate",
    ],
)
def test_proof_refuses_other_execution_or_answer(tmp_path: Path, name: str) -> None:
    assert check(tmp_path, mutate(name)).returncode != 0


def test_terminal_status_not_completed_under_top_level_success_fails_the_proof(
    tmp_path: Path,
) -> None:
    receipt = recorded()
    assert receipt["status"] == "success"
    assert check(tmp_path, receipt).returncode == 0
    refused = check(tmp_path, mutate("terminal_not_completed_under_success"))
    assert refused.returncode != 0
    assert "AssertionError" in refused.stderr


AC1_RECORD_FILE = (
    "tests/nodes/node_delegation_routing_reducer/test_contract_proof_omn24980.py"
)
AC1_RECORD_SELECTOR = "-k ac1_record"


def ac1_binders(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for i in data["dod_evidence"] if "AC1" in i.get("binds_ac", [])]


def is_ac1_record_check(check_value: str) -> bool:
    """Whether a check only reads the contract's AC1 record and exercises no routing."""
    words = shlex.split(check_value)
    return (
        words[:3] == ["uv", "run", "pytest"]
        and [w for w in words if w.endswith(".py")] == [AC1_RECORD_FILE]
        and " ".join(words[-2:]) == AC1_RECORD_SELECTOR
    )


def test_only_the_ac1_record_check_binds_ac1_whatever_its_id() -> None:
    # AC1 is the missing-terminal cause. A routing, receipt-format or health
    # check does not capture that cause, so none may bind AC1 under any id (not
    # only ids naming "ceiling"). The one bound item reads the AC1 record alone.
    data = contract()
    binders = ac1_binders(data)
    assert binders, "AC1 needs a bound item"
    for item in binders:
        assert all(is_ac1_record_check(c["check_value"]) for c in item["checks"]), item[
            "id"
        ]
    proof = next(i for i in data["dod_evidence"] if i["id"] == INTEGRATION_PROOF)
    assert proof.get("binds_ac", []) == []


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest tests/nodes/node_delegation_routing_reducer/test_local_rung_context_ceiling_omn24980.py -q",
        f"uv run pytest {AC1_RECORD_FILE} -q --no-cov",
        f"uv run pytest {AC1_RECORD_FILE} {AC1_RECORD_FILE} -q --no-cov -k ac1_record",
        "echo ok",
    ],
)
def test_ac1_binding_guard_refuses_a_routing_check_under_any_id(command: str) -> None:
    assert not is_ac1_record_check(command)
    data = contract()
    data["dod_evidence"][0]["id"] = "dod-an-id-with-no-routing-word"
    data["dod_evidence"][0]["binds_ac"] = ["AC1"]
    data["dod_evidence"][0]["checks"] = [{"check_value": command}]
    bad = [
        i["id"]
        for i in ac1_binders(data)
        if not all(is_ac1_record_check(c["check_value"]) for c in i["checks"])
    ]
    assert "dod-an-id-with-no-routing-word" in bad


def acceptance_entries(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for requirement in data["requirements"]:
        for entry in requirement["acceptance"]:
            entries[entry["id"]] = entry
    return entries


def test_ac1_record_has_a_rescope_or_a_captured_reproduction() -> None:
    data = contract()
    assert "AC1" in acceptance_entries(data)
    status = data["context"]["criterion_status"]["AC1"]
    rescope = status.get("owner_rescope")
    reproduction = status.get("captured_reproduction")
    assert rescope or reproduction, "AC1 needs an owner re-scope or a reproduction"
    if reproduction:
        # A reproduction names the run, the topic and the consumer group id.
        for key in ("run_id", "topic", "consumer_group", "terminal_class"):
            assert reproduction.get(key), key
    assert status["status"] == "open"


def test_ac2_record_lists_ac2_as_open() -> None:
    data = contract()
    assert "AC2" in acceptance_entries(data)
    assert data["context"]["criterion_status"]["AC2"]["status"] == "open"


def test_ac3_record_lists_ac3_as_open() -> None:
    data = contract()
    assert "AC3" in acceptance_entries(data)
    assert data["context"]["criterion_status"]["AC3"]["status"] == "open"


def test_title_does_not_claim_evidence_is_on_the_bus() -> None:
    data = contract()
    assert "on the bus" not in data["title"].lower()
    other = yaml.safe_load((CONTRACT.parent / "OMN-25102.yaml").read_text())
    assert "on the bus" not in other["title"].lower()
    assert "AC1: OPEN" in data["summary"]
    assert "AC2: OPEN" in data["summary"]
    assert "AC3: OPEN" in data["summary"]
