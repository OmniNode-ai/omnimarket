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

import ast
import copy
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_skill_result_status import EnumSkillResultStatus
from omnibase_core.models.dispatch.model_skill_result import ModelSkillResult
from omnibase_infra.cli.delegate_terminal_resolver import resolve_delegate_terminal
from omnibase_infra.cli.model_receipt_runtime_summary import ModelReceiptRuntimeSummary

pytestmark = pytest.mark.unit
CONTRACT = Path(__file__).parents[3] / "contracts/OMN-24980.yaml"
RECORDED = Path(__file__).parent / "fixtures/delegate_stdout_local.json"
INTEGRATION_PROOF = "dod-omn24980-integration-proof"
RECORD_FILE = (
    "tests/nodes/node_delegation_routing_reducer/test_contract_proof_omn24980.py"
)
PACKAGED_LADDER = yaml.safe_load(
    (
        Path(__file__).parents[3] / "src/omnimarket/configs/routing_tiers.yaml"
    ).read_text()
)
# The rung whose input budget this change raised (it stood at 8192 before).
CHANGED_RUNG = "local-heavy-reasoning"
# The recorded run answered a 39-character prompt on another local rung; the
# proof prompt is 44290 characters (11072 estimated tokens) plus about 364
# tokens of wrapper.
PROOF_INPUT_TOKENS = 11436


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


def answered_by_changed_rung() -> dict[str, Any]:
    """The accepted control: the recorded envelope with the answer a fixed dev ladder gives.

    No recorded run of the proof prompt exists yet (the dev runtime still mounts
    the old ladder), so this control is constructed from the recorded envelope
    by naming the changed rung and the proof prompt's metered input tokens. It is
    the only receipt the proof may accept, and every refusing mutation below
    starts from it.
    """
    receipt = copy.deepcopy(recorded())
    inner = payload(receipt)
    attempt = inner["attempts"][0]
    attempt["backend_id"] = CHANGED_RUNG
    attempt["tier"] = "local"
    inner["metrics"]["input_tokens"] = PROOF_INPUT_TOKENS
    return receipt


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


def test_recorded_envelope_matches_the_skill_result_model() -> None:
    # The stdout envelope is omnibase_core's ModelSkillResult around the
    # summary the CLI builds: every top-level key is a field of the model, the
    # status is a member of its enum and the exit code is its integer field.
    receipt = recorded()
    assert set(receipt) <= set(ModelSkillResult.model_fields)
    assert receipt["result_model"] == (
        "omnibase_infra.cli.model_receipt_runtime_summary.ModelReceiptRuntimeSummary"
    )
    assert EnumSkillResultStatus(receipt["status"]) is EnumSkillResultStatus.SUCCESS
    assert isinstance(receipt["exit_code"], int)
    assert receipt["exit_code"] == 0
    assert receipt["result"]["exit_code"] == 0


def test_proof_accepts_an_answer_from_the_changed_rung(tmp_path: Path) -> None:
    control = answered_by_changed_rung()
    completed = check(tmp_path, control)
    assert completed.returncode == 0, completed.stderr
    assert CHANGED_RUNG in completed.stdout
    local_rungs = next(t for t in PACKAGED_LADDER["tiers"] if t["name"] == "local")
    changed = next(m for m in local_rungs["models"] if m["backend_id"] == CHANGED_RUNG)
    assert changed["max_context_tokens"] > 8192
    assert payload(control)["metrics"]["input_tokens"] > 8192


def test_proof_refuses_the_recorded_373_token_run_by_another_local_rung(
    tmp_path: Path,
) -> None:
    # The recorded real run: local tier, accepted, but answered by a rung that
    # base already answered on, with 373 input tokens. Presented as proof it fails.
    receipt = recorded()
    attempt = payload(receipt)["attempts"][0]
    assert attempt["backend_id"] == "local-omnipc2-chat"
    assert attempt["tier"] == "local"
    assert payload(receipt)["metrics"]["input_tokens"] == 373
    refused = check(tmp_path, receipt)
    assert refused.returncode != 0
    assert "AssertionError" in refused.stderr


def shape_problems(command: str) -> list[str]:
    """What is wrong with how the proof command reaches the runtime."""
    words = shlex.split(command)
    expected = {"--bus": "kafka", "--lane": "dev", "--locus": "deployed-lane"}
    problems = []
    for flag, value in expected.items():
        found = words[words.index(flag) + 1] if flag in words else None
        if found != value:
            problems.append(f"{flag} is {found}, not {value}")
    if any(word.startswith("DELEGATION_ROUTING_TIERS_PATH=") for word in words):
        problems.append("packaged-file override")
    return problems


def test_proof_command_runs_on_the_dev_lane_over_the_bus() -> None:
    assert shape_problems(proof_command()) == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("--lane dev", "--lane stability"),
        ("--lane dev", "--lane prod"),
        ("--bus kafka", "--bus inmemory"),
        ("--locus deployed-lane", "--locus in-process"),
        ("--task-type", "DELEGATION_ROUTING_TIERS_PATH=/x --task-type"),
    ],
)
def test_proof_command_naming_another_lane_or_path_is_refused(
    old: str, new: str
) -> None:
    command = proof_command()
    assert old in command
    assert shape_problems(command.replace(old, new)) != []


def mutate(name: str) -> dict[str, Any]:
    receipt = answered_by_changed_rung()
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
    elif name == "other_local_rung":
        attempt["backend_id"] = "local-omnipc2-chat"
    elif name == "small_prompt":
        inner["metrics"]["input_tokens"] = 373
    elif name == "input_tokens_at_8192":
        inner["metrics"]["input_tokens"] = 8192
    elif name == "metrics_missing":
        del inner["metrics"]["input_tokens"]
    elif name == "two_accepted_attempts":
        inner["attempts"] = [copy.deepcopy(attempt), attempt]
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
        "other_local_rung",
        "small_prompt",
        "input_tokens_at_8192",
        "metrics_missing",
        "two_accepted_attempts",
        "hosted_answer",
        "quality_gate_failed",
        "skipped_local_candidate",
    ],
)
def test_proof_refuses_other_execution_or_answer(tmp_path: Path, name: str) -> None:
    assert check(tmp_path, mutate(name)).returncode != 0


@pytest.mark.parametrize(
    "status", [m.value for m in EnumSkillResultStatus if m.value != "success"]
)
def test_any_top_level_status_but_success_fails_the_proof(
    tmp_path: Path, status: str
) -> None:
    receipt = answered_by_changed_rung()
    assert check(tmp_path, receipt).returncode == 0
    receipt["status"] = status
    assert check(tmp_path, receipt).returncode != 0


@pytest.mark.parametrize("exit_code", [1, 2, 75])
def test_any_nonzero_top_level_or_summary_exit_code_fails_the_proof(
    tmp_path: Path, exit_code: int
) -> None:
    top = answered_by_changed_rung()
    top["exit_code"] = exit_code
    assert check(tmp_path, top).returncode != 0
    inner = answered_by_changed_rung()
    inner["result"]["exit_code"] = exit_code
    assert check(tmp_path, inner).returncode != 0


def test_terminal_status_not_completed_under_top_level_success_fails_the_proof(
    tmp_path: Path,
) -> None:
    receipt = answered_by_changed_rung()
    assert receipt["status"] == "success"
    assert check(tmp_path, receipt).returncode == 0
    refused = check(tmp_path, mutate("terminal_not_completed_under_success"))
    assert refused.returncode != 0
    assert "AssertionError" in refused.stderr


OPEN_CRITERIA = ("AC1", "AC2", "AC3")
RECORD_COMMANDS = [
    f"uv run pytest {RECORD_FILE} -q --no-cov -k ac{n}_record" for n in (1, 2, 3)
]


def bound_open_criteria(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Every (item id, criterion) that binds a criterion this contract lists as open.

    The Done gate counts a passing bound check as proof that its criterion is
    met (durable_evidence_gate reads ``binds_ac`` and refuses only a criterion
    no item binds), so a bound check that passes while the criterion is unmet
    would let the ticket close with the criterion open.
    """
    status = data["context"]["criterion_status"]
    return [
        (item["id"], label)
        for item in data["dod_evidence"]
        for label in item.get("binds_ac", [])
        if status.get(label, {}).get("status") == "open"
    ]


def open_asserting_test_names() -> set[str]:
    """Names of this file's tests that assert a criterion's status is open."""
    tree = ast.parse(Path(__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            source = ast.get_source_segment(Path(__file__).read_text(), node) or ""
            if '["status"] == "open"' in source:
                names.add(node.name)
    return names


def test_no_check_binds_ac1_ac2_or_ac3_while_they_are_open() -> None:
    data = contract()
    for label in OPEN_CRITERIA:
        assert data["context"]["criterion_status"][label]["status"] == "open"
    assert bound_open_criteria(data) == []
    # No item binds the three at all, under any id.
    bound = {
        label for item in data["dod_evidence"] for label in item.get("binds_ac", [])
    }
    assert not bound & set(OPEN_CRITERIA)
    proof = next(i for i in data["dod_evidence"] if i["id"] == INTEGRATION_PROOF)
    assert proof.get("binds_ac", []) == []


@pytest.mark.parametrize("label", OPEN_CRITERIA)
@pytest.mark.parametrize("command", RECORD_COMMANDS)
def test_binding_an_open_criterion_to_a_status_open_check_is_refused(
    label: str, command: str
) -> None:
    # Positive control: the record checks exist and select tests that assert
    # status open. Bound to an open criterion they would pass while it is unmet.
    selector = command.rsplit("-k ", 1)[1]
    assert any(selector in name for name in open_asserting_test_names())
    data = contract()
    item = data["dod_evidence"][0]
    item["binds_ac"] = [label]
    item["checks"] = [{"check_value": command}]
    assert bound_open_criteria(data) == [(item["id"], label)]


def listed_criteria(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The open criteria this contract lists, by label, with the criterion text.

    They sit under ``context`` and not under ``requirements[].acceptance``: the
    push hook refuses a contract that declares a criterion no item binds, and
    none of these three may be bound while it is open.
    """
    listed = data["context"]["criterion_status"]
    for label, entry in listed.items():
        assert entry["statement"].startswith(f"{label}: "), label
    assert data["requirements"][0]["acceptance"] == []
    return dict(listed)


def test_ac1_record_has_a_rescope_or_a_captured_reproduction() -> None:
    data = contract()
    assert "AC1" in listed_criteria(data)
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
    assert "AC2" in listed_criteria(data)
    assert data["context"]["criterion_status"]["AC2"]["status"] == "open"


def test_ac3_record_lists_ac3_as_open() -> None:
    data = contract()
    assert "AC3" in listed_criteria(data)
    assert data["context"]["criterion_status"]["AC3"]["status"] == "open"


def test_title_does_not_claim_evidence_is_on_the_bus() -> None:
    data = contract()
    assert "on the bus" not in data["title"].lower()
    other = yaml.safe_load((CONTRACT.parent / "OMN-25102.yaml").read_text())
    assert "on the bus" not in other["title"].lower()
    assert "AC1: OPEN" in data["summary"]
    assert "AC2: OPEN" in data["summary"]
    assert "AC3: OPEN" in data["summary"]


def test_follow_ups_do_not_read_as_having_no_owner_ticket_yet() -> None:
    summary = " ".join(contract()["summary"].split()).lower()
    assert "no owner ticket yet" not in summary
    assert "not yet filed as tickets" in summary
    assert "mounted ladder" in summary
    assert "local-share alarm" in summary


def test_reproduction_is_limited_to_summarization_in_the_contract() -> None:
    summary = " ".join(contract()["summary"].split())
    assert "summarization call on a single-slot endpoint" in summary
    assert "no other class was reproduced" in summary
