# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18700: the served model's conformance bar is a number, per contract.

Four properties the ticket's criteria and its E11 falsifier require, each
tested against the live runner with the wrapper replaced by a recorded
terminal in the wire model's own shape:

* each trial grades the served local model's FIRST answer and names why it
  failed, so a contract failure is never reported as a quality-gate miss;
* each contract carries a numeric rate and its counts, never only a verdict;
* the run can target an in-process locus (the lab host a per-run overlay
  names) and checks which endpoint answered;
* the receipt records the invocation that regenerates it.
"""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.delegation import response_contract_conformance_runner
from omnimarket.delegation.response_contract_conformance_runner import (
    FAILURE_CLASSES,
    SlotGuard,
    run_live_manifest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)

_MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/configs/delegation_response_contract_conformance.v1.json"
)
_MODEL = "Qwen3.8-27B"
_LAB_ENDPOINT = "http://gpu-b.lab.invalid:8000/v1/chat/completions"
_OTHER_ENDPOINT = "http://gpu-a.lab.invalid:8000/v1/chat/completions"
_CLOUD_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

_CONTENT = {
    "reasoning": '{"category":"ruling","confidence":0.95}',
    "document": "## Result\n\nThe requested deliverable.",
    "summarization": "The requested plain-text deliverable.",
}


def _manifest() -> dict[str, object]:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _single_contract_manifest(index: int = 0, trials: int = 1) -> dict[str, object]:
    manifest = _manifest()
    contracts = manifest["contracts"]
    assert isinstance(contracts, list)
    contract = contracts[index]
    assert isinstance(contract, dict)
    contract["trials"] = trials
    manifest["contracts"] = [contract]
    return manifest


def _contract_sha(task_type: str, contract: dict[str, object]) -> str:
    resolved = (
        response_contract_conformance_runner.resolve_task_class_deliverable_contract(
            task_type, contract
        )
    )
    return response_contract_conformance_runner.canonical_deliverable_contract_sha256(
        resolved
    )


def _attempt(
    *,
    tier: str = "local",
    model_id: str = _MODEL,
    decision: str | None = "accept",
    reason: str | None = "quality_bar_met",
    passed: bool = True,
    failure_class: str | None = None,
) -> dict[str, object]:
    return {
        "tier": tier,
        "backend_id": f"{tier}-rung",
        "model_id": model_id,
        "quality_gate_passed": passed,
        "quality_score": 1.0 if passed else 0.0,
        "failure_class": failure_class,
        "acceptance_decision": decision,
        "acceptance_reason": reason,
        "acceptance_detail": "" if passed else "SCHEMA_VIOLATION: <root>: detail",
    }


def _terminal(
    command: list[str],
    *,
    attempts: list[dict[str, object]],
    provider: str = _LAB_ENDPOINT,
    model_name: str = _MODEL,
    response: str | None = None,
    validated: bool = True,
    quality_gate_passed: bool = True,
) -> dict[str, Any]:
    task_type = command[command.index("--task-type") + 1]
    contract = json.loads(command[command.index("--response-contract") + 1])
    output_shape = contract.get("x-omninode-output-shape", "json")
    terminal = {
        "status": "completed",
        "correlation_id": str(uuid4()),
        "task_type": task_type,
        "response": _CONTENT[task_type] if response is None else response,
        "preamble_chars": 0,
        "quality_gate_passed": quality_gate_passed,
        "provider": provider,
        "model_name": model_name,
        "attempts": attempts,
        "attempts_count": len(attempts),
        "response_contract_evidence": {
            "conveyed": True,
            "validated": validated,
            "output_shape": output_shape,
            "contract_sha256": _contract_sha(task_type, contract),
            "channel": "messages[0].content",
        },
        "budget_evidence": {
            "requested_timeout_seconds": 30,
            "task_class_timeout_ceiling_seconds": 240,
            "execution_timeout_seconds": 30,
            "terminal_delivery_margin_seconds": 60,
        },
    }
    return ModelDelegateSkillResponse.model_validate(terminal).model_dump(mode="json")


@pytest.fixture
def trusted_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    workspace_root = tmp_path / "trusted-workspace"
    wrapper = workspace_root / "omnibase_infra" / "scripts" / "onex"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(
        response_contract_conformance_runner,
        "_workspace_root_from_repository",
        lambda: workspace_root,
    )
    monkeypatch.setenv("OMNI_HOME", str(workspace_root))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)
    return workspace_root


def _serve(
    monkeypatch: pytest.MonkeyPatch, build: Any, calls: list[list[str]] | None = None
) -> None:
    def completed(command: list[str], **_: object) -> CompletedProcess[str]:
        if calls is not None:
            calls.append(command)
        stdout = json.dumps({"run_id": str(uuid4()), "result": build(command)})
        return CompletedProcess(args=command, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(
        response_contract_conformance_runner.subprocess, "run", completed
    )


def _only_trial(receipt: dict[str, Any]) -> dict[str, Any]:
    contract = receipt["contracts"][0]
    trials = contract["trials"]
    assert len(trials) == 1
    trial = trials[0]
    assert isinstance(trial, dict)
    return trial


@pytest.mark.unit
def test_served_model_answering_the_contract_is_a_conformant_trial(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    _serve(monkeypatch, lambda c: _terminal(c, attempts=[_attempt()]))

    receipt = run_live_manifest(
        _single_contract_manifest(),
        timeout_seconds=30,
        locus="in-process",
        expected_endpoint_host="gpu-b.lab.invalid",
    )

    trial = _only_trial(receipt)
    assert trial["passed"] is True
    assert trial["failure_class"] is None
    assert trial["served_endpoint"] == _LAB_ENDPOINT
    contract = receipt["contracts"][0]
    assert contract["pass_rate"] == 1.0
    assert contract["conformant_trials"] == 1


@pytest.mark.unit
def test_a_rejected_local_answer_on_the_contract_is_a_contract_failure_not_a_bar_miss(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    """The twelve-of-twelve shape: local rejected on the contract, cloud answers.

    Before this, the trial read as a quality-gate miss or as a pass from the
    cloud rung. It is the served model failing the declared contract.
    """
    _serve(
        monkeypatch,
        lambda c: _terminal(
            c,
            provider=_CLOUD_ENDPOINT,
            model_name="cloud-model",
            attempts=[
                _attempt(
                    decision="climb", reason="deterministic_floor_failed", passed=False
                ),
                _attempt(tier="cheap_cloud", model_id="cloud-model"),
            ],
        ),
    )

    receipt = run_live_manifest(
        _single_contract_manifest(), timeout_seconds=30, locus="in-process"
    )

    trial = _only_trial(receipt)
    assert trial["passed"] is False
    assert trial["failure_class"] == "contract_nonconformant"
    assert trial["failure_family"] == "model_contract"
    assert trial["local_acceptance_reason"] == "deterministic_floor_failed"
    counts = receipt["contracts"][0]["failure_counts"]
    assert counts["contract_nonconformant"] == 1
    assert counts["quality_gate_miss"] == 0


@pytest.mark.unit
def test_a_local_answer_rejected_off_the_contract_is_a_quality_gate_miss(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    _serve(
        monkeypatch,
        lambda c: _terminal(
            c,
            provider=_CLOUD_ENDPOINT,
            model_name="cloud-model",
            attempts=[
                _attempt(
                    decision="climb", reason="score_below_required_bar", passed=False
                ),
                _attempt(tier="cheap_cloud", model_id="cloud-model"),
            ],
        ),
    )

    receipt = run_live_manifest(
        _single_contract_manifest(), timeout_seconds=30, locus="in-process"
    )

    trial = _only_trial(receipt)
    assert trial["failure_class"] == "quality_gate_miss"
    assert trial["failure_family"] == "model_quality"
    assert receipt["contracts"][0]["failure_counts"]["contract_nonconformant"] == 0


@pytest.mark.unit
def test_accepted_bytes_that_fail_the_output_only_bar_are_a_delivery_failure(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    """K5's bar: the caller-visible bytes are the object, with nothing before it."""
    _serve(
        monkeypatch,
        lambda c: _terminal(
            c,
            attempts=[_attempt()],
            response='Here is the answer.\n{"category":"ruling","confidence":0.9}',
        ),
    )

    receipt = run_live_manifest(
        _single_contract_manifest(), timeout_seconds=30, locus="in-process"
    )

    trial = _only_trial(receipt)
    assert trial["passed"] is False
    assert trial["failure_class"] == "output_bar_nonconformant"
    assert trial["failure_family"] == "delivery"


@pytest.mark.unit
def test_no_attempt_by_the_served_model_is_not_measured_and_not_a_pass(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    _serve(
        monkeypatch,
        lambda c: _terminal(
            c,
            provider=_CLOUD_ENDPOINT,
            model_name="cloud-model",
            attempts=[_attempt(tier="cheap_cloud", model_id="cloud-model")],
        ),
    )

    receipt = run_live_manifest(
        _single_contract_manifest(), timeout_seconds=30, locus="in-process"
    )

    trial = _only_trial(receipt)
    assert trial["failure_class"] == "served_model_not_observed"
    assert trial["failure_family"] == "run"
    contract = receipt["contracts"][0]
    assert contract["pass_rate"] == 0.0
    assert contract["measured_trials"] == 0
    assert contract["measured_pass_rate"] is None


@pytest.mark.unit
def test_an_answer_from_another_endpoint_than_the_expected_host_is_not_measured(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    _serve(
        monkeypatch,
        lambda c: _terminal(c, provider=_OTHER_ENDPOINT, attempts=[_attempt()]),
    )

    receipt = run_live_manifest(
        _single_contract_manifest(),
        timeout_seconds=30,
        locus="in-process",
        expected_endpoint_host="gpu-b.lab.invalid",
    )

    trial = _only_trial(receipt)
    assert trial["passed"] is False
    assert trial["failure_class"] == "served_model_not_observed"
    assert trial["served_endpoint"] == _OTHER_ENDPOINT


@pytest.mark.unit
def test_a_transport_failure_on_the_served_model_is_a_run_failure(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    _serve(
        monkeypatch,
        lambda c: _terminal(
            c,
            provider=_CLOUD_ENDPOINT,
            model_name="cloud-model",
            attempts=[
                _attempt(
                    decision=None,
                    reason=None,
                    passed=False,
                    failure_class="model_unavailable",
                ),
                _attempt(tier="cheap_cloud", model_id="cloud-model"),
            ],
        ),
    )

    receipt = run_live_manifest(
        _single_contract_manifest(), timeout_seconds=30, locus="in-process"
    )

    trial = _only_trial(receipt)
    assert trial["failure_class"] == "served_model_call_failed"
    assert trial["failure_family"] == "run"
    assert trial["local_failure_class"] == "model_unavailable"


@pytest.mark.unit
def test_each_contract_reports_a_rate_and_counts_for_every_failure_class(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    """AC1 and AC2: per-contract numbers that can rank two models."""
    outcomes = iter(
        [
            [_attempt()],
            [
                _attempt(
                    decision="climb", reason="deterministic_floor_failed", passed=False
                ),
                _attempt(tier="cheap_cloud", model_id="cloud-model"),
            ],
            [_attempt()],
            [_attempt()],
        ]
    )

    def build(command: list[str]) -> dict[str, Any]:
        attempts = next(outcomes)
        local_answered = attempts[-1]["tier"] == "local"
        return _terminal(
            command,
            attempts=attempts,
            provider=_LAB_ENDPOINT if local_answered else _CLOUD_ENDPOINT,
            model_name=_MODEL if local_answered else "cloud-model",
        )

    _serve(monkeypatch, build)

    receipt = run_live_manifest(
        _single_contract_manifest(trials=4), timeout_seconds=30, locus="in-process"
    )

    contract = receipt["contracts"][0]
    assert contract["contract_id"] == "l11-json-classifier"
    assert contract["trials_run"] == 4
    assert contract["conformant_trials"] == 3
    assert contract["pass_rate"] == 0.75
    assert contract["measured_trials"] == 4
    assert contract["measured_pass_rate"] == 0.75
    assert set(contract["failure_counts"]) == set(FAILURE_CLASSES)
    assert contract["failure_counts"]["contract_nonconformant"] == 1
    assert sum(contract["failure_counts"].values()) == 1
    # 0.75 is below the manifest's 0.95 minimum, so the contract is below bar.
    assert contract["passed"] is False


@pytest.mark.unit
def test_in_process_locus_dispatches_here_and_never_names_a_lane(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    calls: list[list[str]] = []
    _serve(monkeypatch, lambda c: _terminal(c, attempts=[_attempt()]), calls)

    run_live_manifest(
        _single_contract_manifest(), timeout_seconds=30, locus="in-process"
    )

    assert len(calls) == 1
    command = calls[0]
    assert command[command.index("--locus") + 1] == "in-process"
    assert command[command.index("--bus") + 1] == "inmemory"
    assert "--lane" not in command
    state_root = command[command.index("--state-root") + 1]
    assert state_root == str(trusted_workspace.resolve() / ".onex_state")


@pytest.mark.unit
def test_deployed_lane_locus_is_unchanged_by_default(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    calls: list[list[str]] = []
    _serve(monkeypatch, lambda c: _terminal(c, attempts=[_attempt()]), calls)

    run_live_manifest(_single_contract_manifest(), timeout_seconds=30)

    command = calls[0]
    assert command[command.index("--locus") + 1] == "deployed-lane"
    assert command[command.index("--bus") + 1] == "kafka"
    assert command[command.index("--lane") + 1] == "dev"


@pytest.mark.unit
def test_unknown_locus_is_refused_before_any_dispatch(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    monkeypatch.setattr(
        response_contract_conformance_runner.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("must not dispatch"),
    )
    with pytest.raises(ValueError, match="locus"):
        run_live_manifest(_single_contract_manifest(), timeout_seconds=30, locus="x")


@pytest.mark.unit
def test_receipt_records_the_invocation_that_regenerates_it(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path, tmp_path: Path
) -> None:
    """AC4: the rate is reproducible from the recorded invocation alone."""
    overlay = tmp_path / "bifrost_overrides.lab.yaml"
    overlay.write_text("backends: []\n", encoding="utf-8")
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    _serve(monkeypatch, lambda c: _terminal(c, attempts=[_attempt()]))
    argv = ["scripts/ci/run_delegation_response_contract_conformance.py", "--x"]

    receipt = run_live_manifest(
        _single_contract_manifest(trials=3),
        timeout_seconds=30,
        locus="in-process",
        expected_endpoint_host="gpu-b.lab.invalid",
        trials_override=1,
        invocation_argv=argv,
        manifest_path="src/omnimarket/configs/example.json",
        source_revision="0123abcd",
    )

    invocation = receipt["invocation"]
    assert invocation["argv"] == argv
    assert invocation["locus"] == "in-process"
    assert invocation["bus"] == "inmemory"
    assert invocation["timeout_seconds"] == 30
    assert invocation["trials_override"] == 1
    assert invocation["expected_endpoint_host"] == "gpu-b.lab.invalid"
    assert invocation["manifest_sha256"] == receipt["manifest_sha256"]
    assert invocation["overlay"] == {
        "env": "BIFROST_OVERLAY_PATH",
        "file_name": overlay.name,
        "sha256": response_contract_conformance_runner._file_sha256(overlay),
    }
    assert isinstance(invocation["started_at"], str)
    assert isinstance(invocation["finished_at"], str)
    assert invocation["routing_contract"] is None
    assert invocation["source_revision"] == "0123abcd"
    assert invocation["manifest_path"] == "src/omnimarket/configs/example.json"
    contract = receipt["contracts"][0]
    assert contract["trials_run"] == 1
    delegate_argv = contract["delegate_argv"]
    assert delegate_argv[0:3] == [
        "bash",
        "$OMNI_HOME/omnibase_infra/scripts/onex",
        "delegate",
    ]
    assert str(trusted_workspace) not in json.dumps(delegate_argv)
    trial = _only_trial(receipt)
    assert isinstance(trial["run_id"], str)


@pytest.mark.unit
def test_slot_guard_waits_for_a_free_slot_before_each_send(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    busy_reads = iter([3, 2, 1])
    sleeps: list[float] = []
    monkeypatch.setattr(
        response_contract_conformance_runner,
        "_read_busy_slots",
        lambda _url: next(busy_reads),
    )
    monkeypatch.setattr(
        response_contract_conformance_runner.time, "sleep", sleeps.append
    )
    calls: list[list[str]] = []
    _serve(monkeypatch, lambda c: _terminal(c, attempts=[_attempt()]), calls)

    receipt = run_live_manifest(
        _single_contract_manifest(),
        timeout_seconds=30,
        locus="in-process",
        slot_guard=SlotGuard(url="http://host:8000/slots", max_busy=2, wait_seconds=60),
    )

    assert len(calls) == 1
    assert len(sleeps) == 2
    assert receipt["invocation"]["slot_guard"] == {
        "url": "http://host:8000/slots",
        "max_busy": 2,
        "wait_seconds": 60,
    }


@pytest.mark.unit
def test_slot_guard_aborts_without_sending_when_the_server_stays_busy(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    monkeypatch.setattr(
        response_contract_conformance_runner, "_read_busy_slots", lambda _url: 4
    )
    clock = iter(float(n) for n in range(0, 1000, 20))
    monkeypatch.setattr(
        response_contract_conformance_runner.time, "monotonic", lambda: next(clock)
    )
    monkeypatch.setattr(
        response_contract_conformance_runner.time, "sleep", lambda _s: None
    )
    monkeypatch.setattr(
        response_contract_conformance_runner.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("a busy server must not be sent a trial"),
    )

    with pytest.raises(RuntimeError, match="busy"):
        run_live_manifest(
            _single_contract_manifest(),
            timeout_seconds=30,
            locus="in-process",
            slot_guard=SlotGuard(
                url="http://host:8000/slots", max_busy=2, wait_seconds=60
            ),
        )
