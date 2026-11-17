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
import hashlib
import json
import re
import shlex
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pydantic
import pytest
import yaml
from omnibase_core.enums.enum_skill_result_status import EnumSkillResultStatus
from omnibase_core.models.dispatch.model_skill_result import ModelSkillResult
from omnibase_infra.cli import cli_delegate
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
# The proof prompt is the generated code text the contract's command prints
# (40587 characters, 10146 estimated tokens). Metered by the in-process run on
# the branch ladder, run be2e4a4f-41a4-4d25-a6f7-dce7ca4f0597, answered by
# local-heavy-reasoning: 16912 input tokens (fixtures/metered_token_readings.json).
# A repeated-character prompt of 44290 characters meters 5597 and never passes.
READINGS = json.loads(
    (Path(__file__).parent / "fixtures/metered_token_readings.json").read_text()
)
PROOF_INPUT_TOKENS = 16912


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


def test_recorded_envelope_validates_against_the_model_the_cli_prints() -> None:
    # The CLI prints omnibase_core's ModelSkillResult (cli_delegate imports it
    # from omnibase_core.models.dispatch.model_skill_result), not an
    # omnibase_infra model: the same class object, validated as a whole.
    assert cli_delegate.ModelSkillResult is ModelSkillResult
    assert ModelSkillResult.__module__.startswith("omnibase_core.")
    receipt = recorded()
    validated = ModelSkillResult.model_validate(receipt)
    assert validated.status is EnumSkillResultStatus.SUCCESS
    assert validated.exit_code == 0
    assert receipt["result_model"] == (
        "omnibase_infra.cli.model_receipt_runtime_summary.ModelReceiptRuntimeSummary"
    )
    ModelReceiptRuntimeSummary.model_validate(receipt["result"])
    assert receipt["result"]["exit_code"] == 0
    # Negative control: a body the model refuses is refused by model_validate
    # (key names alone would have passed it).
    broken = copy.deepcopy(receipt)
    broken["exit_code"] = "not-an-int"
    with pytest.raises(pydantic.ValidationError):
        ModelSkillResult.model_validate(broken)


def test_proof_accepts_an_answer_from_the_changed_rung(tmp_path: Path) -> None:
    control = answered_by_changed_rung()
    completed = check(tmp_path, control)
    assert completed.returncode == 0, completed.stderr
    assert CHANGED_RUNG in completed.stdout
    local_rungs = next(t for t in PACKAGED_LADDER["tiers"] if t["name"] == "local")
    changed = next(m for m in local_rungs["models"] if m["backend_id"] == CHANGED_RUNG)
    assert changed["max_context_tokens"] > 8192
    assert payload(control)["metrics"]["input_tokens"] > 8192


def test_proof_accepts_the_spread_peer_above_8192_tokens_only(tmp_path: Path) -> None:
    # The dev run is spread across the rung and its peer by a hash of the
    # correlation id (what the tests strip), so a peer answer above 8192 metered
    # tokens is the changed ladder working; a peer answer at 373 is not.
    peer = answered_by_changed_rung()
    payload(peer)["attempts"][0]["backend_id"] = "local-omnipc2-chat"
    assert check(tmp_path, peer).returncode == 0
    payload(peer)["metrics"]["input_tokens"] = 373
    assert check(tmp_path, peer).returncode != 0


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
        # A local rung outside the changed rung's spread group.
        attempt["backend_id"] = "local-coder"
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


CRITERIA = ("AC1", "AC2", "AC3")


def unbound_declared(data: dict[str, Any]) -> tuple[str, ...]:
    """The declared criteria no item binds, read by the verifier's own rule."""
    from omnimarket.nodes.node_dod_verify.services.ac_falsifier_checks import (
        derive_falsifier_items,
    )

    _, summary = derive_falsifier_items(
        data,
        data["dod_evidence"],
        repo_candidates=["omnimarket"],
        path_exists=lambda _repo, _path: True,
        declared_runner=lambda _repo, _path: "uv run pytest",
    )
    return tuple(summary.unbound_criteria)


def bound_items(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    bound: dict[str, list[dict[str, Any]]] = {}
    for item in data["dod_evidence"]:
        for label in item.get("binds_ac", []):
            bound.setdefault(label, []).append(item)
    return bound


def test_ac1_ac2_and_ac3_are_declared_and_each_is_bound() -> None:
    data = contract()
    declared = data["requirements"][0]["acceptance"]
    assert [entry["id"] for entry in declared] == list(CRITERIA)
    for entry in declared:
        assert entry["statement"].startswith(f"{entry['id']}: ")
        assert "falsifier:" in entry["statement"]
    assert unbound_declared(data) == ()
    assert sorted(bound_items(data)) == list(CRITERIA)


@pytest.mark.parametrize("label", CRITERIA)
def test_removing_a_binding_is_detected_by_the_verifiers_rule(label: str) -> None:
    # Positive control, then the mutation: with the binding removed the same
    # rule reports the criterion unbound. A test that sets the field it then
    # reads back (the earlier shape) could not fail; this one fails when the
    # binds_ac entry is missing from the real contract.
    data = contract()
    assert unbound_declared(data) == ()
    for item in data["dod_evidence"]:
        item["binds_ac"] = [a for a in item.get("binds_ac", []) if a != label]
    assert unbound_declared(data) == (label,)


def test_an_empty_acceptance_list_is_what_the_old_contract_declared() -> None:
    # The refusal this contract answers: with no declared criterion the
    # verifier has nothing to bind and the ticket would close on tests alone.
    data = contract()
    data["requirements"][0]["acceptance"] = []
    assert unbound_declared(data) == ()
    assert contract()["requirements"][0]["acceptance"] != []


def test_bound_checks_run_at_the_local_done_gate_and_do_not_assert_open_status() -> (
    None
):
    data = contract()
    for label, items in bound_items(data).items():
        for item in items:
            assert item["execution_scope"] == "local_done_gate", label
            for check_spec in item["checks"]:
                value = check_spec["check_value"]
                assert "ac1_record" not in value
                assert "-k ac" not in value
                assert RECORD_FILE not in value
    ac2 = bound_items(data)["AC2"][0]["checks"][0]["check_value"]
    assert ":8001/v1/models" in ac2
    assert "curl -sf" in ac2  # --fail: the exit turns on the HTTP status
    assert "overlays" in bound_items(data)["AC3"][0]["checks"][0]["check_value"]


def test_ac2_check_fails_until_port_8001_answers_a_model_list_with_200() -> None:
    command = bound_items(contract())["AC2"][0]["checks"][0]["check_value"]
    env = {
        "PATH": "/usr/bin:/bin",
        "BIFROST_LOCAL_CODER_ENDPOINT_URL": "http://127.0.0.1:1/v1/chat/completions",
    }

    def run(text: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", text],
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )

    # Nothing serves port 8001: curl is refused, the check fails.
    refused = run(command)
    assert refused.returncode != 0
    assert "PASS" not in refused.stdout

    # Positive control: a server answering the model list with 200 on the port
    # the check reads (the port swapped for an ephemeral one).
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"object": "list", "data": []}')

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        swapped = command.replace(":8001", f":{server.server_port}")
        assert swapped != command
        served = run(swapped)
        assert served.returncode == 0, served.stderr
        assert "PASS" in served.stdout
    finally:
        server.shutdown()
        server.server_close()


def test_ac1_ac3_checks_name_the_dev_lane_and_the_overlays_test() -> None:
    data = contract()
    ac1 = bound_items(data)["AC1"][0]["checks"][0]["check_value"]
    assert shape_problems(ac1) == []
    assert 'a[0]["tier"] == "local"' in ac1
    ac3 = bound_items(data)["AC3"][0]["checks"][0]["check_value"]
    assert "tests/test_local_endpoint_hosts_serve_models.py" in ac3


def test_ac1_record_has_a_rescope_or_a_captured_reproduction() -> None:
    status = contract()["context"]["criterion_status"]["AC1"]
    rescope = status.get("owner_rescope")
    reproduction = status.get("captured_reproduction")
    assert rescope or reproduction, "AC1 needs an owner re-scope or a reproduction"
    if reproduction:
        for key in ("run_id", "topic", "consumer_group", "terminal_class"):
            assert reproduction.get(key), key
    assert status["status"] == "open"


def test_criteria_stay_open_in_the_record() -> None:
    status = contract()["context"]["criterion_status"]
    for label in CRITERIA:
        assert status[label]["status"] == "open"


def test_title_does_not_claim_evidence_is_on_the_bus() -> None:
    data = contract()
    assert "on the bus" not in data["title"].lower()
    other = yaml.safe_load((CONTRACT.parent / "OMN-25102.yaml").read_text())
    assert "on the bus" not in other["title"].lower()
    for label in CRITERIA:
        assert f"{label}: OPEN" in data["summary"]


def test_follow_ups_name_their_owner_tickets_or_say_none_exists() -> None:
    data = contract()
    summary = " ".join(data["summary"].split()).lower()
    assert "no owner ticket yet" not in summary
    assert "not yet filed as tickets" not in summary
    owners = data["context"]["owner_tickets"]
    assert set(owners) == {"dev_mounted_ladder", "local_share_alarm"}
    for name, entry in owners.items():
        ticket = entry["ticket"]
        if ticket is None:
            # No ticket exists: the contract says so and names who files it. An
            # id is never invented to fill the field.
            assert "no owner ticket exists" in entry["state"], name
            assert "orchestrator files" in entry["state"], name
        else:
            assert re.fullmatch(r"OMN-\d+", ticket), name
    assert "mounted ladder" in summary
    assert "local-share alarm" in summary
    assert "context.owner_tickets" in data["summary"]
    # Every ticket id the contract text names is one of the ids this lane read.
    named = set(re.findall(r"OMN-\d+", yaml.safe_dump(data)))
    assert named <= {"OMN-24980", "OMN-25098"} | {
        e["ticket"] for e in owners.values() if e["ticket"]
    }


def test_the_proof_prompt_is_run_and_its_metered_size_is_the_recorded_one() -> None:
    command = proof_command()
    match = re.search(r"\$\(python3 -c '(.*?)'\)\"", command, re.S)
    assert match, "the proof command builds its prompt with python3 -c"
    run = subprocess.run(
        [sys.executable, "-c", match.group(1)],
        check=True,
        text=True,
        capture_output=True,
    )
    prompt = run.stdout.rstrip("\n")
    reading = READINGS["proof_prompt"]
    assert len(prompt) == reading["characters"] == 40587
    assert reading["estimated_tokens"] == len(prompt) // 4
    assert hashlib.sha256(prompt.encode()).hexdigest() == reading["prompt_sha256"]
    # Past the old 8192 budget by the router's estimate and by the endpoint's
    # meter, and inside the new 35000 budget: the proof can pass.
    assert 8192 < len(prompt) // 4 <= 35000
    branch_runs = reading["runs"]["branch_ladder_in_process"]
    assert {r["input_tokens"] for r in branch_runs} == {PROOF_INPUT_TOKENS}
    assert all(r["tier"] == "local" for r in branch_runs)
    assert reading["runs"]["dev_deployed_lane_old_ladder"]["tier"] == "cheap_frontier"
    # The repeated-character prompt this replaces cannot pass at all.
    assert READINGS["x_repeated_proof_prompt"]["metered_input_tokens"] < 8192
    assert PROOF_INPUT_TOKENS > 8192


def test_local_omnipc2_chat_selection_and_ceiling_are_recorded() -> None:
    peer = contract()["context"]["local_omnipc2_chat"]
    assert "spread" in peer["what_selects_it"]
    assert "hash of the correlation id" in peer["what_selects_it"]
    assert "49152" in peer["ceiling"]


def test_reproduction_is_limited_to_summarization_in_the_contract() -> None:
    summary = " ".join(contract()["summary"].split())
    assert "summarization call on a single-slot endpoint" in summary
    assert "no other class was reproduced" in summary
