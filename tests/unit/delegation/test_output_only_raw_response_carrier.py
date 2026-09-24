# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19385: the output-only bar judges a live trial from the retained raw bytes.

The D1 bar (OMN-18932) needs the raw provider response to decide whether the
caller's bytes needed extraction. The runtime's count of leading characters cut
cannot see anything after a JSON value, so before this change every clean JSON
trial was ``output_only_evidence_absent`` and no JSON contract could reach its
minimum pass rate.

The raw response rides the terminal inside the provider-boundary evidence
(``response_contract_evidence.raw_response``, the Core carrier
``ModelDelegationRawResponse``). These tests hand the live runner a terminal in
the wire model's own shape, with that carrier added to the serialized
evidence, and check that the bar is decided on the raw bytes:

* a JSON answer the provider followed with prose is refused, and scored as the
  served model's own output-only failure;
* a JSON answer the provider returned alone is measured and passes;
* a carrier whose text does not hash to its own sha256 is a delivery defect,
  never silently used or silently ignored;
* a carrier over the Core bound (hash and length only) decides nothing, so the
  trial falls back to the runtime count exactly as before.

The carrier's own shape, bound and JSON round trip through the terminal are
pinned in omnibase_core
``tests/unit/models/delegation/wire/test_delegation_raw_response.py``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.delegation import response_contract_conformance_runner
from omnimarket.delegation.response_contract_conformance_runner import (
    FAILURE_CLASSES,
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
_JSON_ANSWER = '{"category":"ruling","confidence":0.95}'
_SOURCE_FIELD = "choices[0].message.content"


def _json_manifest() -> dict[str, object]:
    manifest: dict[str, object] = json.loads(_MANIFEST_PATH.read_text("utf-8"))
    contracts = manifest["contracts"]
    assert isinstance(contracts, list)
    contract = contracts[0]
    assert isinstance(contract, dict)
    assert contract["output_shape"] == "json"
    contract["trials"] = 1
    manifest["contracts"] = [contract]
    return manifest


def _carrier(text: str) -> dict[str, object]:
    encoded = text.encode("utf-8")
    return {
        "source_field": _SOURCE_FIELD,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "utf8_bytes": len(encoded),
        "text": text,
    }


def _terminal(
    command: list[str], *, raw_response: dict[str, object] | None
) -> dict[str, Any]:
    task_type = command[command.index("--task-type") + 1]
    contract = json.loads(command[command.index("--response-contract") + 1])
    resolved = (
        response_contract_conformance_runner.resolve_task_class_deliverable_contract(
            task_type, contract
        )
    )
    terminal = {
        "status": "completed",
        "correlation_id": str(uuid4()),
        "task_type": task_type,
        "response": _JSON_ANSWER,
        "preamble_chars": 0,
        "quality_gate_passed": True,
        "provider": _LAB_ENDPOINT,
        "model_name": _MODEL,
        "attempts": [
            {
                "tier": "local",
                "backend_id": "local-rung",
                "model_id": _MODEL,
                "quality_gate_passed": True,
                "quality_score": 1.0,
                "acceptance_decision": "accept",
                "acceptance_reason": "quality_bar_met",
            }
        ],
        "attempts_count": 1,
        "response_contract_evidence": {
            "conveyed": True,
            "validated": True,
            "output_shape": "json",
            "contract_sha256": (
                response_contract_conformance_runner.canonical_deliverable_contract_sha256(
                    resolved
                )
            ),
            "channel": "messages[0].content",
        },
        "budget_evidence": {
            "requested_timeout_seconds": 30,
            "task_class_timeout_ceiling_seconds": 240,
            "execution_timeout_seconds": 30,
            "terminal_delivery_margin_seconds": 60,
        },
    }
    serialized = ModelDelegateSkillResponse.model_validate(terminal).model_dump(
        mode="json"
    )
    if raw_response is not None:
        # The carrier as the wire delivers it: nested in the evidence object.
        serialized["response_contract_evidence"]["raw_response"] = raw_response
    return serialized


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


def _run_one(
    monkeypatch: pytest.MonkeyPatch, raw_response: dict[str, object] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    def completed(command: list[str], **_: object) -> CompletedProcess[str]:
        stdout = json.dumps(
            {
                "run_id": str(uuid4()),
                "result": _terminal(command, raw_response=raw_response),
            }
        )
        return CompletedProcess(args=command, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(
        response_contract_conformance_runner.subprocess, "run", completed
    )
    receipt = run_live_manifest(
        _json_manifest(), timeout_seconds=30, locus="in-process"
    )
    contract = receipt["contracts"][0]
    trials = contract["trials"]
    assert len(trials) == 1
    return contract, trials[0]


@pytest.mark.unit
def test_a_json_answer_the_provider_followed_with_prose_is_refused(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    """RED before OMN-19385: the runner never read the carrier, so this trial
    was ``output_only_evidence_absent`` and not scored at all."""
    raw = f"{_JSON_ANSWER}\n\nI chose ruling because the text states a decision."

    contract, trial = _run_one(monkeypatch, _carrier(raw))

    assert trial["passed"] is False
    assert trial["failure_class"] == "output_only_refused"
    assert trial["failure_family"] == "model_output_only"
    output_only = trial["output_only"]
    assert output_only["evidence_basis"] == "raw_provider_bytes"
    assert output_only["refusals"] == ["extraction_required_trailing_text"]
    assert output_only["raw_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    # A refusal on the raw bytes is a measurement of the served model.
    assert contract["measured_trials"] == 1
    assert contract["measured_pass_rate"] == 0.0


@pytest.mark.unit
def test_a_clean_json_answer_is_measured_and_passes_from_the_raw_bytes(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    """RED before OMN-19385: the same clean answer was evidence-absent."""
    contract, trial = _run_one(monkeypatch, _carrier(_JSON_ANSWER))

    assert trial["passed"] is True
    assert trial["failure_class"] is None
    output_only = trial["output_only"]
    assert output_only["accepted"] is True
    assert output_only["evidence_basis"] == "raw_provider_bytes"
    assert output_only["raw_chars"] == len(_JSON_ANSWER)
    assert trial["raw_response_carrier"] == {
        "present": True,
        "retained": True,
        "sha256_verified": True,
    }
    assert contract["measured_trials"] == 1
    assert contract["measured_pass_rate"] == 1.0
    assert contract["passed"] is True


@pytest.mark.unit
def test_surrounding_whitespace_in_the_raw_bytes_is_not_extraction(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    """The effect trims the content it hands on; the carrier keeps it exact."""
    _, trial = _run_one(monkeypatch, _carrier(f"\n  {_JSON_ANSWER}\n\n"))

    assert trial["passed"] is True
    assert trial["output_only"]["evidence_basis"] == "raw_provider_bytes"


@pytest.mark.unit
def test_a_carrier_whose_text_does_not_hash_to_its_sha256_is_a_delivery_defect(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    carrier = _carrier(f"{_JSON_ANSWER}\n\nAnd a trailing aside.")
    carrier["text"] = _JSON_ANSWER

    contract, trial = _run_one(monkeypatch, carrier)

    assert trial["passed"] is False
    assert trial["failure_class"] == "raw_response_carrier_invalid"
    assert trial["failure_family"] == "delivery"
    assert trial["raw_response_carrier"]["sha256_verified"] is False
    assert "raw_response_carrier_invalid" in FAILURE_CLASSES
    # A delivery defect counts against the contract, like every other defect
    # of our path around a model answer: nothing may pass on mangled evidence.
    assert contract["failure_counts"]["raw_response_carrier_invalid"] == 1
    assert contract["passed"] is False


@pytest.mark.unit
def test_a_carrier_over_the_bound_decides_nothing_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    """Hash and length only: not raw bytes, so the runtime count decides."""
    carrier = _carrier(_JSON_ANSWER)
    carrier["text"] = None
    carrier["utf8_bytes"] = 65537

    _, trial = _run_one(monkeypatch, carrier)

    assert trial["failure_class"] == "output_only_evidence_absent"
    assert trial["output_only"]["evidence_basis"] == "runtime_extraction_count"
    assert trial["raw_response_carrier"] == {
        "present": True,
        "retained": False,
        "sha256_verified": None,
    }


@pytest.mark.unit
def test_no_carrier_is_still_evidence_absent_for_json(
    monkeypatch: pytest.MonkeyPatch, trusted_workspace: Path
) -> None:
    """Positive control: a terminal from a producer that predates the carrier."""
    _, trial = _run_one(monkeypatch, None)

    assert trial["failure_class"] == "output_only_evidence_absent"
    assert trial["output_only"]["refusals"] == ["extraction_evidence_incomplete"]
    assert trial["raw_response_carrier"] == {
        "present": False,
        "retained": False,
        "sha256_verified": None,
    }
