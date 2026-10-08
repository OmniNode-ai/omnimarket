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
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.delegation import response_contract_conformance_runner
from omnimarket.delegation.response_contract_conformance_runner import (
    FAILURE_CLASSES,
    run_live_manifest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    HandlerLlmDelegationCall,
)
from omnimarket.routing import delegation_backend_resolution

_MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/configs/delegation_response_contract_conformance.v1.json"
)
_MODEL = "Qwen3.8-27B"
_LAB_ENDPOINT = "http://gpu-b.lab.invalid:8000/v1/chat/completions"
_JSON_ANSWER = '{"category":"ruling","confidence":0.95}'
_SOURCE_FIELD = "choices[0].message.content"


@pytest.fixture
def provider_boundary() -> Iterator[tuple[str, dict[str, Any], list[dict[str, Any]]]]:
    """Exercise both real HTTP adapters against an isolated provider on loopback."""
    body: dict[str, Any] = {
        "id": "provider-body-field",
        "model": _MODEL,
        "choices": [{"message": {"content": _JSON_ANSWER}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
        "other_provider_field": "not-message-content",
    }
    seen: list[dict[str, Any]] = []

    class Provider(BaseHTTPRequestHandler):
        def reply(self, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            self.reply({"data": [{"id": _MODEL}]})

        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"payload": request, "headers": dict(self.headers)})
            self.reply(body)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions", body, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.unit
@pytest.mark.usefixtures("stub_provider_quota_reader")
@pytest.mark.parametrize(
    "raw",
    [f" \n{_JSON_ANSWER}\né ", "é" * 32768, "é" * 32769],
    ids=["exact-whitespace", "at-utf8-bound", "over-utf8-bound"],
)
def test_provider_boundary_field_set_keeps_only_exact_message_content(
    provider_boundary: tuple[str, dict[str, Any], list[dict[str, Any]]], raw: str
) -> None:
    endpoint, body, seen = provider_boundary
    body["choices"][0]["message"]["content"] = raw
    intent = ModelInferenceIntent(
        base_url=endpoint,
        model=_MODEL,
        system_prompt="Return only JSON.",
        prompt="request-payload-only",
        correlation_id=uuid4(),
        max_tokens=128,
        timeout_seconds=30,
        response_contract_instruction="Return only JSON.",
        response_contract_sha256="a" * 64,
        response_contract_output_shape="json",
        extra_headers={"X-Test-Field": "request-header-only"},
    )
    result = HandlerInferenceIntent().handle(intent)
    assert result.error_message == ""
    assert len(seen) == 1
    assert seen[0]["payload"]["messages"][-1]["content"] == intent.prompt
    assert seen[0]["headers"]["X-Test-Field"] == "request-header-only"
    evidence = result.response_contract_evidence
    assert evidence is not None
    assert evidence.conveyed
    carrier = evidence.raw_response
    assert carrier is not None, "provider boundary dropped the raw response"
    expected = _carrier(raw)
    if len(raw.encode("utf-8")) > 65536:
        expected["text"] = None
    assert carrier.model_dump(mode="json") == expected


@pytest.mark.unit
@pytest.mark.usefixtures("stub_provider_quota_reader")
def test_local_effect_field_set_captures_before_inline_reasoning_is_removed(
    provider_boundary: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    endpoint, body, seen = provider_boundary
    raw = f"private reasoning</think>\n{_JSON_ANSWER}"
    body["choices"][0]["message"]["content"] = raw
    result = HandlerLlmDelegationCall()(
        ModelLlmDelegationCallRequest(
            request_id=str(uuid4()),
            correlation_id=str(uuid4()),
            causation_id=str(uuid4()),
            model_id=_MODEL,
            endpoint_ref=endpoint,
            prompt="request-payload-only",
            prompt_hash="",
            timeout_seconds=30,
            inline_reasoning_terminator="</think>",
            extra_headers={"X-Test-Field": "request-header-only"},
        )
    )
    assert result.success
    assert len(seen) == 1
    assert result.content == _JSON_ANSWER
    assert result.raw_response is not None
    assert result.raw_response.model_dump(mode="json") == _carrier(raw)


@pytest.mark.unit
@pytest.mark.usefixtures("stub_provider_quota_reader")
@pytest.mark.parametrize(
    "trailing", ["", "\n\nI chose ruling because it states a decision."]
)
async def test_local_terminal_retains_raw_bytes_before_json_extraction(
    provider_boundary: tuple[str, dict[str, Any], list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trailing: str,
) -> None:
    endpoint, body, seen = provider_boundary
    raw = f" \n{_JSON_ANSWER}{trailing}\n "
    body["choices"][0]["message"]["content"] = raw
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: [
            {
                "backend_id": "local-coder",
                "endpoint_url": endpoint,
                "model_name": _MODEL,
                "tier": "local",
                "max_tokens": 8192,
                "timeout_ms": 30000,
                "capabilities": ["agent_delegation", "reasoning"],
            }
        ],
    )
    schema = {
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["category", "confidence"],
        "additionalProperties": False,
    }
    handler = HandlerDelegateSkill(
        dispatch_port=LocalDelegationDispatchPort(
            effect_handler=HandlerLlmDelegationCall(),
            evidence_db_path=tmp_path / "delegation.sqlite",
            effect_process_boundary=False,
        )
    )
    terminal = await handler.handle(
        ModelDelegateSkillRequest(
            prompt="Classify this source: a decision is stated. Return category and confidence.",
            task_type="document",
            source="codex",
            backend_id="local-coder",
            response_contract=schema,
        )
    )
    assert terminal.status == "completed"
    assert len(seen) == 1
    assert json.loads(terminal.response or "") == json.loads(_JSON_ANSWER)
    round_trip = ModelDelegateSkillResponse.model_validate_json(
        terminal.model_dump_json()
    )
    evidence = round_trip.response_contract_evidence
    assert evidence is not None
    assert evidence.raw_response is not None, "local terminal dropped the raw response"
    assert evidence.raw_response.model_dump(mode="json") == _carrier(raw)
    verdict = response_contract_conformance_runner.evaluate_output_only(
        raw_response=evidence.raw_response.text,
        caller_bytes=terminal.response or "",
        contract=response_contract_conformance_runner.resolve_task_class_deliverable_contract(
            "document", schema
        ),
    )
    assert verdict.evidence_basis == "raw_provider_bytes"
    assert verdict.accepted is (not trailing)


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


# ---------------------------------------------------------------------------
# A JSON value the runtime re-serialized is not extraction.
#
# Measured on the lab-served model (mac-scratch, 2026-09-24, in-process path,
# correlation ec13a90d-cab3-42cc-95f9-e6a6aeb8dd25): the provider returned one
# pretty-printed JSON value and nothing else, and the in-process port handed
# the caller its canonical re-serialization (OMN-7942). The caller's bytes are
# then not a byte slice of the raw response, and the bar refused a clean
# answer as ``caller_bytes_not_a_raw_provider_slice``. For a JSON deliverable,
# "no extraction" means the raw response is exactly one JSON value equal to
# the caller's, not that the bytes coincide.
# ---------------------------------------------------------------------------

_LAB_RAW_PRETTY = '{\n  "category": "none",\n  "confidence": 1\n}'
_LAB_CALLER_CANONICAL = '{"category": "none", "confidence": 1}'


def _json_contract() -> Any:
    manifest = _json_manifest()
    contracts = manifest["contracts"]
    assert isinstance(contracts, list)
    entry = contracts[0]
    assert isinstance(entry, dict)
    return response_contract_conformance_runner.resolve_task_class_deliverable_contract(
        entry["task_type"], entry["response_contract"]
    )


@pytest.mark.unit
def test_a_re_serialized_sole_json_value_is_not_extraction() -> None:
    from omnimarket.delegation.output_only_acceptance import evaluate_output_only

    verdict = evaluate_output_only(
        raw_response=_LAB_RAW_PRETTY,
        caller_bytes=_LAB_CALLER_CANONICAL,
        contract=_json_contract(),
    )

    assert verdict.accepted is True
    assert verdict.evidence_basis == "raw_provider_bytes"


@pytest.mark.unit
def test_a_re_serialized_json_value_followed_by_prose_is_still_refused() -> None:
    from omnimarket.delegation.output_only_acceptance import evaluate_output_only

    verdict = evaluate_output_only(
        raw_response=f"{_LAB_RAW_PRETTY}\n\nNo decision text was supplied.",
        caller_bytes=_LAB_CALLER_CANONICAL,
        contract=_json_contract(),
    )

    assert verdict.accepted is False
    assert verdict.refusals


@pytest.mark.unit
def test_a_json_value_of_another_type_is_not_the_same_value() -> None:
    """``true`` and ``1`` are equal in Python and different JSON values."""
    from omnimarket.delegation.output_only_acceptance import evaluate_output_only

    verdict = evaluate_output_only(
        raw_response='{"category": "none", "confidence": true}',
        caller_bytes='{"category": "none", "confidence": 1}',
        contract=_json_contract(),
    )

    assert verdict.accepted is False
