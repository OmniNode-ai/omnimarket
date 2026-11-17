# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the contract's receipt assertion against a recorded CLI receipt and its variants.

The receipt is the stdout of a real ``onex delegate --json`` run on the dev lane
(``fixtures/delegate_stdout_local.json``, run a5f1ef93-d5c1-4535-8211-dc8cedc83fda,
read 2026-11-17T07:37Z, host paths, addresses and the bus host redacted). It is the
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
from datetime import datetime, timedelta
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

from omnimarket.handlers.routing.input_token_bound import (
    LOCAL_MEMBER_INPUT_BOUNDS,
    input_token_upper_bound,
)

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
# A repeated-character prompt of 44313 characters meters 5597 and never passes.
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


def key_order_problems(receipt: dict[str, Any]) -> list[str]:
    """Where the recorded envelope departs from what the CLI prints."""
    problems = []
    printed = list(ModelSkillResult.model_fields)
    if list(receipt) != printed:
        problems.append(f"top-level keys {list(receipt)} are not the printed order")
    identity = receipt.get("runtime_identity")
    terminal = receipt["result"]["terminal_payload"]["payload"]
    stamped = identity and identity.get("stamped_at")
    if not stamped:
        problems.append("runtime_identity carries no stamped_at")
    else:
        # The CLI stamps the identity as it prints, a moment after the terminal.
        gap = datetime.fromisoformat(stamped) - datetime.fromisoformat(
            receipt["result"]["terminal_payload"]["envelope_timestamp"]
        )
        if not timedelta(0) <= gap <= timedelta(seconds=60):
            problems.append(f"runtime_identity stamped {gap} after the terminal")
    assert terminal["status"] == "completed"
    return problems


def test_recorded_envelope_is_the_cli_stdout_in_printed_key_order() -> None:
    receipt = recorded()
    # The CLI prints the model's fields in declaration order, runtime_identity
    # before schema_version, stamped as it prints.
    assert key_order_problems(receipt) == []
    assert "runtime_identity" in ModelSkillResult.model_fields
    validated = ModelSkillResult.model_validate(receipt)
    assert str(validated.run_id) == receipt["run_id"]


def test_hand_added_or_reordered_runtime_identity_is_refused() -> None:
    receipt = recorded()
    # A key-sorted dump with runtime_identity appended last (the earlier fixture).
    reordered = {k: receipt[k] for k in sorted(receipt) if k != "runtime_identity"}
    reordered["runtime_identity"] = receipt["runtime_identity"]
    assert any("printed order" in p for p in key_order_problems(reordered))
    # An identity stamped hours after the run (the earlier fixture: 05:58Z on a
    # 02:01Z run).
    late = copy.deepcopy(receipt)
    late["runtime_identity"]["stamped_at"] = "2026-11-17T12:00:00.000000Z"
    assert any("stamped" in p for p in key_order_problems(late))
    missing = copy.deepcopy(receipt)
    del missing["runtime_identity"]["stamped_at"]
    assert key_order_problems(missing) != []


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


def test_proof_refuses_the_recorded_373_token_run_below_8192_tokens(
    tmp_path: Path,
) -> None:
    # The recorded real run: local tier, accepted, answered by the changed rung
    # but with 373 input tokens (a 39-character prompt). Presented as proof of
    # the changed ladder it fails: the budget change is not what served it.
    receipt = recorded()
    attempt = payload(receipt)["attempts"][0]
    assert attempt["backend_id"] == CHANGED_RUNG
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


def live_item(data: dict[str, Any], label: str) -> dict[str, Any]:
    """The local Done gate item that checks open criterion ``label`` (it binds none)."""
    item_id = f"dod-omn24980-{label.lower()}-live"
    return next(i for i in data["dod_evidence"] if i["id"] == item_id)


def test_open_criteria_are_recorded_and_no_local_done_gate_item_binds_one() -> None:
    # The repository rule (OMN-22403, tests/ci): a local_done_gate item may not
    # carry binds_ac, and every declared criterion must be bound to a hosted
    # test_passes check. A hosted check cannot fail for an open criterion without
    # holding the pull request red, so AC1 to AC3 are recorded, not declared.
    for name in ("OMN-24980.yaml", "OMN-25284.yaml"):
        data = yaml.safe_load((CONTRACT.parent / name).read_text())
        assert not [
            i["id"]
            for i in data["dod_evidence"]
            if i.get("execution_scope") == "local_done_gate" and i.get("binds_ac")
        ], name
        declared = {
            ac["id"]
            for requirement in data["requirements"]
            for ac in requirement["acceptance"]
        }
        assert declared.isdisjoint(data["context"]["open_criteria"]), name
        for statement in data["context"]["open_criteria"].values():
            assert "falsifier:" in statement
    data = contract()
    assert data["requirements"][0]["acceptance"] == []
    assert sorted(data["context"]["open_criteria"]) == list(CRITERIA)
    for label in CRITERIA:
        item = live_item(data, label)
        assert item["execution_scope"] == "local_done_gate"
        assert "binds_ac" not in item
        assert data["context"]["open_criteria"][label].startswith(f"{label}: ")


def test_binding_an_open_criterion_to_a_local_done_gate_item_is_refused() -> None:
    # Positive control for the rule above: the contract this one replaced bound
    # AC1 to AC3 to local_done_gate items, and the verifier's own rule refuses it.
    from omnimarket.nodes.node_dod_verify.services.scoped_item_binding import (
        scoped_item_binding_violation,
    )

    data = contract()
    for label in CRITERIA:
        item = copy.deepcopy(live_item(data, label))
        assert scoped_item_binding_violation(item) is None
        item["binds_ac"] = [label]
        refusal = scoped_item_binding_violation(item)
        assert refusal is not None, label
        assert "local_done_gate" in refusal, label


def test_the_open_criteria_of_the_review_ticket_are_recorded_and_bind_nothing() -> None:
    # OMN-25284 AC1 (literal owner ticket ids) and AC5 (one passing hosted dev
    # run) cannot be met by this change; their items must not pass for them.
    review = yaml.safe_load((CONTRACT.parent / "OMN-25284.yaml").read_text())
    assert sorted(review["context"]["open_criteria"]) == ["AC1", "AC5"]
    declared = {ac["id"] for ac in review["requirements"][0]["acceptance"]}
    assert declared == {"AC2", "AC3", "AC4", "AC6", "AC7", "AC8", "AC9"}
    bound = {label for i in review["dod_evidence"] for label in i.get("binds_ac", [])}
    assert bound == declared
    # The record that the criteria are open: no owner ticket and no hosted run.
    record = contract()["context"]
    assert any(e["ticket"] is None for e in record["owner_tickets"].values())
    assert record["dev_ladder_run"]["passing_hosted_run"] is None


def test_bound_checks_run_at_the_local_done_gate_and_do_not_assert_open_status() -> (
    None
):
    data = contract()
    for label in CRITERIA:
        item = live_item(data, label)
        assert item["execution_scope"] == "local_done_gate", label
        for check_spec in item["checks"]:
            value = check_spec["check_value"]
            assert "ac1_record" not in value
            assert "-k ac" not in value
            assert RECORD_FILE not in value
    ac2 = live_item(data, "AC2")["checks"][0]["check_value"]
    assert ":8001/v1/models" in ac2
    assert "curl -sf" in ac2  # --fail: the exit turns on the HTTP status
    assert "overlays" in live_item(data, "AC3")["checks"][0]["check_value"]


def test_ac2_check_fails_until_port_8001_answers_a_model_list_with_200() -> None:
    command = live_item(contract(), "AC2")["checks"][0]["check_value"]
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
    ac1 = live_item(data, "AC1")["checks"][0]["check_value"]
    assert shape_problems(ac1) == []
    assert 'a[0]["tier"] == "local"' in ac1
    ac3 = live_item(data, "AC3")["checks"][0]["check_value"]
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
    # meter, and inside the input bound of the smallest local member (the spread
    # peer's 48128), which is what gates it: the 35000 estimate never binds a
    # prompt the bound admits, so it is not the check.
    assert len(prompt) // 4 > 8192
    assert (
        input_token_upper_bound(prompt)
        <= LOCAL_MEMBER_INPUT_BOUNDS["local-omnipc2-chat"]
        == 48128
    )
    assert (
        input_token_upper_bound(prompt)
        >= reading["runs"]["branch_ladder_in_process"][0]["input_tokens"]
    )
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


def ac1_check() -> str:
    return str(live_item(contract(), "AC1")["checks"][0]["check_value"])


GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}
CAUSE_TEST = """\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from calc import add


def test_add() -> None:
    assert add(1, 2) == 3
"""


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", **GIT_ENV},
    )
    return done.stdout.strip()


def owning_repo(
    root: Path,
    *,
    test_body: str,
    base_fixed: bool = False,
    head_fixed: bool = True,
    name: str = "owning_repo",
) -> tuple[Path, str]:
    """A git repository with a base commit and a head commit; returns it and the base sha."""
    repo = root / name
    (repo / "tests").mkdir(parents=True)
    git(root, "init", "-q", str(repo))
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n"
        if base_fixed
        else "def add(a, b):\n    return a - b\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n"
        if head_fixed
        else "def add(a, b):\n    return a - b\n"
    )
    (repo / "tests/test_cause.py").write_text(test_body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fix")
    return repo, base


def run_ac1_check(
    root: Path,
    data: dict[str, Any],
    receipt: dict[str, Any],
    *,
    test_body: str | None = None,
    base_fixed: bool = False,
    head_fixed: bool = True,
    repo_name: str = "owning_repo",
) -> subprocess.CompletedProcess[str]:
    """Run the AC1 check in a stand-in home whose CLI prints ``receipt``.

    ``test_body`` is the owning repository's recorded test, committed at the head
    of a real two-commit git repository (the base commit holds no test file).
    """
    home = root / "stand_in_home"
    cli = home / "omnibase_infra/scripts/onex"
    cli.parent.mkdir(parents=True)
    printed = home / "printed.json"
    printed.write_text(json.dumps(receipt))
    cli.write_text(f'#!/bin/sh\ncat "{printed}"\n')
    cli.chmod(0o755)
    (home / ".onex_state").mkdir()
    if test_body is not None:
        repo, base = owning_repo(
            home,
            test_body=test_body,
            base_fixed=base_fixed,
            head_fixed=head_fixed,
            name=repo_name,
        )
        status = data["context"]["criterion_status"]["AC1"]
        if not status.get("cause_fix_test"):
            status["cause_fix_test"] = {
                "repo": repo.name,
                "path": "tests/test_cause.py",
                "base_ref": base,
            }
    work = root / "repo"
    (work / "contracts").mkdir(parents=True)
    (work / "contracts/OMN-24980.yaml").write_text(yaml.safe_dump(data))
    return subprocess.run(
        ["bash", "-c", ac1_check()],
        check=False,
        text=True,
        capture_output=True,
        env={
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "OMNI_HOME": str(home),
            "ONEX_PYTEST": f"{sys.executable} -m pytest",
            **GIT_ENV,
        },
        cwd=work,
    )


def test_a_one_line_summarise_run_at_tier_local_does_not_meet_ac1(
    tmp_path: Path,
) -> None:
    # The recorded run is a one-line summarise run on the dev lane accepted at
    # tier local. The receipt half of the AC1 check accepts it, yet AC1 (the cause
    # named and fixed, with a test that fails on base in the owning repository)
    # is open, so the whole check must fail until that test is recorded.
    run = recorded()
    attempt = payload(run)["attempts"][0]
    assert attempt["tier"] == "local"
    assert attempt["acceptance_decision"] == "accept"
    data = contract()
    assert data["context"]["criterion_status"]["AC1"]["cause_fix_test"] is None
    refused = run_ac1_check(tmp_path / "open", data, run)
    assert refused.returncode != 0
    assert "no base-failing test of the cause" in refused.stderr
    assert "PASS" not in refused.stdout
    # Positive control: with a test that fails at the base commit and passes at
    # the head recorded, the same recorded run satisfies the check, so only the
    # cause test held it back.
    served = run_ac1_check(tmp_path / "control", contract(), run, test_body=CAUSE_TEST)
    assert served.returncode == 0, served.stderr
    assert "PASS" in served.stdout


def test_ac1_cause_test_must_fail_at_base_ref_and_pass_at_the_head(
    tmp_path: Path,
) -> None:
    run = recorded()
    # An empty test file exists and has the three strings, but runs no test: it
    # exits 5 at the head, so it does not satisfy the check (it once did).
    empty = run_ac1_check(tmp_path / "empty", contract(), run, test_body="")
    assert empty.returncode != 0
    assert "PASS" not in empty.stdout
    assert "must fail with exit 1 at base_ref" in empty.stderr
    # A test that already passes at the base commit shows no cause.
    passing_at_base = run_ac1_check(
        tmp_path / "base", contract(), run, test_body=CAUSE_TEST, base_fixed=True
    )
    assert passing_at_base.returncode != 0
    assert "must fail with exit 1 at base_ref" in passing_at_base.stderr
    # A test that still fails at the head is not a fix.
    failing_at_head = run_ac1_check(
        tmp_path / "head", contract(), run, test_body=CAUSE_TEST, head_fixed=False
    )
    assert failing_at_head.returncode != 0
    assert "does not pass at the head" in failing_at_head.stderr
    assert "PASS" not in failing_at_head.stdout
    # A recorded test file that is not in the owning repository is refused.
    data = contract()
    ok = run_ac1_check(tmp_path / "ok", data, run, test_body=CAUSE_TEST)
    assert ok.returncode == 0, ok.stderr
    data = contract()
    data["context"]["criterion_status"]["AC1"]["cause_fix_test"] = {
        "repo": "owning_repo",
        "path": "tests/test_missing.py",
        "base_ref": "HEAD",
    }
    missing = run_ac1_check(tmp_path / "missing", data, run, test_body=CAUSE_TEST)
    assert missing.returncode != 0
    assert "does not exist" in missing.stderr


def owner_ticket_problems(data: dict[str, Any]) -> list[str]:
    """What is wrong with how the contract names the owners of its follow-ups."""
    problems = []
    for name, entry in data["context"]["owner_tickets"].items():
        ticket = entry["ticket"]
        if ticket is None:
            # An id is never invented to fill the field: with no ticket the
            # contract says so, names who files it and what was searched.
            if "no owner ticket exists" not in entry.get("state", ""):
                problems.append(
                    f"{name}: null ticket with no statement that none exists"
                )
            if "orchestrator files" not in entry.get("state", ""):
                problems.append(f"{name}: null ticket names no one who files it")
            if not entry.get("searched"):
                problems.append(f"{name}: null ticket records no search")
        elif not re.fullmatch(r"OMN-\d+", str(ticket)):
            problems.append(f"{name}: {ticket!r} is not a ticket id")
    return problems


def test_owner_tickets_are_literal_ids_or_a_recorded_absence() -> None:
    data = contract()
    assert data["ticket_id"] == "OMN-24980"
    assert (
        yaml.safe_load((CONTRACT.parent / "OMN-25214.yaml").read_text())["ticket_id"]
        == "OMN-25214"
    )
    assert owner_ticket_problems(data) == []
    # Mutations: a null with no statement is refused, a malformed id is refused,
    # and a literal id passes.
    bare = contract()
    bare["context"]["owner_tickets"]["dev_mounted_ladder"] = {"ticket": None}
    assert owner_ticket_problems(bare) != []
    wrong = contract()
    wrong["context"]["owner_tickets"]["dev_mounted_ladder"]["ticket"] = "owner TBD"
    assert owner_ticket_problems(wrong) != []
    filed = contract()
    for entry in filed["context"]["owner_tickets"].values():
        entry["ticket"] = "OMN-25098"
    assert owner_ticket_problems(filed) == []


def test_the_dev_run_record_is_a_hosted_local_run_or_says_none_exists() -> None:
    record = contract()["context"]["dev_ladder_run"]
    passing = record["passing_hosted_run"]
    if passing is None:
        # No passing hosted run: say so, and the in-process runs do not count.
        assert "no passing hosted dev run" in record["state"]
        assert record["last_hosted_read"]["locus"] == "deployed-lane"
        assert record["last_hosted_read"]["lane"] == "dev"
        assert record["last_hosted_read"]["bus"] == "kafka"
        assert record["last_hosted_read"]["proof_exit_code"] != 0
        assert record["last_hosted_read"]["tier"] != "local"
    else:
        assert passing["locus"] == "deployed-lane"
        assert passing["lane"] == "dev"
        assert passing["tier"] == "local"
        assert passing["backend_id"] in (CHANGED_RUNG, "local-omnipc2-chat")
        assert passing["input_tokens"] > 8192
    in_process = {
        run["run_id"]
        for run in READINGS["proof_prompt"]["runs"]["branch_ladder_in_process"]
    }
    assert set(record["in_process_runs_do_not_count"]) == in_process
    assert passing is None or passing["run_id"] not in in_process
    # The integration proof stays unbound and local-gate-only while it is open.
    item = next(i for i in contract()["dod_evidence"] if i["id"] == INTEGRATION_PROOF)
    assert "binds_ac" not in item
    assert item["execution_scope"] == "local_done_gate"


LARGE_WINDOW_CLAIM = re.compile(
    r"(is|are)? ?tested (against|through) the large-window rungs"
    r"|tests escalation, research and reasoning (against|through) the large-window rungs",
    re.I,
)


def large_window_claims(text: str) -> bool:
    return LARGE_WINDOW_CLAIM.search(" ".join(text.split())) is not None


def test_no_contract_says_escalation_and_research_are_tested_on_large_window_rungs() -> (
    None
):
    for name in ("OMN-24980.yaml", "OMN-25102.yaml"):
        assert not large_window_claims((CONTRACT.parent / name).read_text()), name
    # Positive controls: both old wordings are what the pattern finds.
    assert large_window_claims(
        "tests escalation, research and reasoning through the large-window rungs."
    )
    assert large_window_claims(
        "each of escalation, research and reasoning is tested against the large-window rungs --"
    )


def test_the_character_count_of_the_repeated_prompt_is_the_metered_one() -> None:
    metered = READINGS["x_repeated_proof_prompt"]["characters"]
    text = " ".join(CONTRACT.read_text().split())
    assert re.findall(r"repeated-character prompt of (\d+) characters", text) == [
        str(metered)
    ]
    assert "44290" not in text
    # Negative control: the count the review found in the contract differs.
    assert metered != 44290


def test_sibling_contract_texts_agree_with_the_bound_criteria() -> None:
    for name in ("OMN-25167.yaml", "OMN-25185.yaml"):
        text = " ".join((CONTRACT.parent / name).read_text().split())
        assert "no longer carry binds_ac" not in text, name
        assert "binds AC1, AC2 or AC3 under any id" not in text, name
        assert "input budget of 64512" not in text, name
        assert "373-token local-omnipc2-chat run is refused" not in text, name
