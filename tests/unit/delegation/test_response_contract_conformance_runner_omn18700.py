# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""L11 fixture receipts prove capture plus validation, never score alone."""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from omnimarket.delegation import response_contract_conformance_runner
from omnimarket.delegation.response_contract_conformance_runner import (
    run_live_manifest,
    run_manifest,
)

_MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/configs/delegation_response_contract_conformance.v1.json"
)


def _manifest() -> dict[str, object]:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _resolved_contract_sha256(task_type: str, contract: dict[str, object]) -> str:
    resolved = (
        response_contract_conformance_runner.resolve_task_class_deliverable_contract(
            task_type, contract
        )
    )
    return response_contract_conformance_runner.canonical_deliverable_contract_sha256(
        resolved
    )


@pytest.mark.unit
def test_l11_manifest_emits_five_deterministic_local_receipts_per_contract() -> None:
    receipt = run_manifest(_manifest())

    assert receipt["passed"] is True
    contracts = receipt["contracts"]
    assert isinstance(contracts, list)
    assert {contract["contract_id"] for contract in contracts} == {
        "l11-json-classifier",
        "markdown-deliverable",
        "plain-text-deliverable",
    }
    for contract in contracts:
        assert contract["pass_rate"] == 1.0
        trials = contract["trials"]
        assert isinstance(trials, list)
        assert len(trials) == 5
        assert all(trial["conveyed"] is True for trial in trials)
        assert all(trial["validated"] is True for trial in trials)
        assert all(trial["passed"] is True for trial in trials)


@pytest.mark.unit
def test_l11_manifest_receipt_run_ids_are_reproducible() -> None:
    first = run_manifest(_manifest())
    second = run_manifest(_manifest())

    assert first == second


@pytest.mark.unit
def test_l11_capture_without_the_instruction_refuses_even_a_valid_answer() -> None:
    manifest = _manifest()
    contracts = manifest["contracts"]
    assert isinstance(contracts, list)
    classifier = contracts[0]
    assert isinstance(classifier, dict)
    capture = classifier["provider_request_capture"]
    assert isinstance(capture, dict)
    payload = capture["payload"]
    assert isinstance(payload, dict)
    messages = payload["messages"]
    assert isinstance(messages, list)
    assert isinstance(messages[0], dict)
    messages[0]["content"] = "A system prompt without the response contract."

    receipt = run_manifest(manifest)

    classifier_receipt = receipt["contracts"][0]
    assert classifier_receipt["pass_rate"] == 0.0
    assert all(trial["validated"] is True for trial in classifier_receipt["trials"])
    assert all(trial["conveyed"] is False for trial in classifier_receipt["trials"])
    assert classifier_receipt["passed"] is False


@pytest.mark.unit
def test_l11_disabled_conveyance_key_guessing_is_a_negative_control() -> None:
    receipt = run_manifest(_manifest())

    classifier = receipt["contracts"][0]
    negative = classifier["negative_control"]
    assert negative == {
        "mode": "conveyance_disabled",
        "validated": False,
        "passed": True,
    }


@pytest.mark.unit
def test_live_runner_refuses_to_pass_without_an_actual_terminal_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wrapper success without receipt evidence cannot become an L11 pass."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setattr(
        response_contract_conformance_runner.subprocess,
        "run",
        lambda *args, **_: CompletedProcess(args=args, returncode=0, stdout="{}"),
    )

    receipt = run_live_manifest(_manifest(), timeout_seconds=1)

    assert receipt["passed"] is False
    assert all(contract["pass_rate"] == 0.0 for contract in receipt["contracts"])
    assert all(
        trial["failure"] == "terminal_evidence_absent"
        for contract in receipt["contracts"]
        for trial in contract["trials"]
    )


@pytest.mark.unit
def test_live_runner_requires_real_terminal_evidence_and_accepts_removed_preamble(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Raw preamble removal is valid when the returned content matches its span."""
    manifest = _manifest()
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    def completed_process(command: list[str], **_: object) -> CompletedProcess[str]:
        task_type = command[command.index("--task-type") + 1]
        contract = json.loads(command[command.index("--response-contract") + 1])
        output_shape = contract.get("x-omninode-output-shape", "json")
        content = {
            "reasoning": '{"category":"ruling","confidence":0.95}',
            "document": "## Result\n\nThe requested deliverable.",
            "summarization": "The requested plain-text deliverable.",
        }[task_type]
        prefix = "removed reasoning\n"
        terminal = {
            "run_id": f"run-{task_type}",
            "content": content,
            "preamble_chars": len(prefix),
            "quality_passed": True,
            "cost_tier_name": "local",
            "model_used": "Qwen3.8-27B",
            "response_contract_evidence": {
                "conveyed": True,
                "validated": True,
                "output_shape": output_shape,
                "contract_sha256": _resolved_contract_sha256(task_type, contract),
                "channel": "messages[0].content",
            },
            "budget_evidence": {
                "requested_timeout_seconds": 30,
                "task_class_timeout_ceiling_seconds": 60,
                "execution_timeout_seconds": 30,
                "terminal_delivery_margin_seconds": 5,
            },
        }
        return CompletedProcess(
            args=command, returncode=0, stdout=json.dumps({"terminal": terminal})
        )

    monkeypatch.setattr(
        response_contract_conformance_runner.subprocess, "run", completed_process
    )

    receipt = run_live_manifest(manifest, timeout_seconds=1)

    assert receipt["passed"] is True
    assert isinstance(receipt["manifest_sha256"], str)
    for contract in receipt["contracts"]:
        assert contract["pass_rate"] == 1.0
        assert all(trial["raw_preamble_chars"] > 0 for trial in contract["trials"])
        assert all(
            trial["preamble_evidence_valid"] is True for trial in contract["trials"]
        )


@pytest.mark.unit
def test_live_runner_rejects_a_terminal_without_preamble_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = _manifest()
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    def completed_process(command: list[str], **_: object) -> CompletedProcess[str]:
        contract = json.loads(command[command.index("--response-contract") + 1])
        terminal = {
            "run_id": "run-missing-preamble",
            "content": '{"category":"ruling","confidence":0.95}',
            "quality_passed": True,
            "cost_tier_name": "local",
            "model_used": "Qwen3.8-27B",
            "response_contract_evidence": {
                "conveyed": True,
                "validated": True,
                "output_shape": "json",
                "contract_sha256": _resolved_contract_sha256("reasoning", contract),
                "channel": "messages[0].content",
            },
            "budget_evidence": {
                "requested_timeout_seconds": 30,
                "task_class_timeout_ceiling_seconds": 60,
                "execution_timeout_seconds": 30,
                "terminal_delivery_margin_seconds": 5,
            },
        }
        return CompletedProcess(
            args=command, returncode=0, stdout=json.dumps({"terminal": terminal})
        )

    monkeypatch.setattr(
        response_contract_conformance_runner.subprocess, "run", completed_process
    )

    receipt = run_live_manifest(manifest, timeout_seconds=1)

    assert receipt["passed"] is False
    first = receipt["contracts"][0]
    assert first["trials"][0]["preamble_evidence_valid"] is False
    assert first["trials"][0]["passed"] is False


@pytest.mark.unit
def test_live_runner_records_typed_predispatch_budget_refusals_separately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    terminal = {
        "run_id": "run-predispatch-refusal",
        "status": "failed",
        "budget_refusal": {
            "reason": "timeout_exceeds_task_class_ceiling",
            "task_type": "reasoning",
            "requested_timeout_seconds": 120,
            "task_class_timeout_ceiling_seconds": 60,
        },
    }
    monkeypatch.setattr(
        response_contract_conformance_runner.subprocess,
        "run",
        lambda *args, **_: CompletedProcess(
            args=args, returncode=0, stdout=json.dumps({"terminal": terminal})
        ),
    )

    receipt = run_live_manifest(_manifest(), timeout_seconds=1)

    assert receipt["passed"] is False
    first_trial = receipt["contracts"][0]["trials"][0]
    assert first_trial["failure"] == "predispatch_budget_refusal"
    assert first_trial["run_id"] == "run-predispatch-refusal"


@pytest.mark.unit
def test_predispatch_budget_refusal_requires_requested_timeout_above_ceiling() -> None:
    receipt = response_contract_conformance_runner._predispatch_budget_refusal_receipt(
        {"run_id": "run-invalid-refusal", "status": "failed"},
        {
            "reason": "timeout_exceeds_task_class_ceiling",
            "task_type": "reasoning",
            "requested_timeout_seconds": 60,
            "task_class_timeout_ceiling_seconds": 60,
        },
        "reasoning",
        0,
    )

    assert receipt["failure"] == "invalid_budget_refusal"
    assert receipt["passed"] is False


@pytest.mark.unit
def test_live_runner_decodes_typed_refusal_from_nonzero_wrapper_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    terminal = {
        "run_id": "run-nonzero-refusal",
        "status": "failed",
        "budget_refusal": {
            "reason": "timeout_exceeds_task_class_ceiling",
            "task_type": "reasoning",
            "requested_timeout_seconds": 241,
            "task_class_timeout_ceiling_seconds": 240,
        },
    }
    monkeypatch.setattr(
        response_contract_conformance_runner.subprocess,
        "run",
        lambda *args, **_: CompletedProcess(
            args=args,
            returncode=1,
            stdout=json.dumps({"terminal": terminal}),
            stderr="typed predispatch refusal",
        ),
    )

    receipt = run_live_manifest(_manifest(), timeout_seconds=240)

    first_trial = receipt["contracts"][0]["trials"][0]
    assert first_trial["failure"] == "predispatch_budget_refusal"
    assert first_trial["run_id"] == "run-nonzero-refusal"
    assert first_trial["wrapper_exit_code"] == 1


@pytest.mark.unit
def test_budget_honoured_requires_execution_to_equal_requested_timeout() -> None:
    assert (
        response_contract_conformance_runner._budget_is_honoured(
            {
                "requested_timeout_seconds": 240,
                "task_class_timeout_ceiling_seconds": 240,
                "execution_timeout_seconds": 239,
                "terminal_delivery_margin_seconds": 60,
            }
        )
        is False
    )
