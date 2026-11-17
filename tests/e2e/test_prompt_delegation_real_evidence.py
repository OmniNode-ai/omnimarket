# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required native real-run readback, separate from fake-CLI process controls.

Failure modes: missing artifacts, failed/vetoed receipt, unknown/zero usage,
unmatched or duplicate stored events, wrong model, and native Claude inference.
This re-reads captured execution artifacts; it does not run inference itself.
"""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.release_proof]


def test_required_prompt_delegation_real_evidence(tmp_path: Path) -> None:
    if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") != "1":
        pytest.skip("Explicit AC33 completion requires real execution artifacts")
    evidence: dict[str, object] = {"status": "failed", "inference_rerun": False}
    artifact = tmp_path / "prompt-real-evidence.json"
    try:
        names = (
            "ONEX_PROMPT_PROOF_RECEIPT",
            "ONEX_PROMPT_PROOF_DATABASE",
            "ONEX_PROMPT_PROOF_CLAUDE",
            "ONEX_PROMPT_PROOF_MODEL",
        )
        missing = [name for name in names if not os.environ.get(name)]
        assert not missing, f"Required AC33 real evidence missing: {missing}"
        paths = [Path(os.environ[name]) for name in names[:3]]
        assert all(path.is_file() for path in paths), "AC33 artifact file missing"
        receipt_path, database_path, native_path = paths
        document = json.loads(receipt_path.read_text())
        assert document["status"] == "success", "Receipt envelope failed"
        receipt = document.get("receipt", document)
        result = receipt["result"]
        run_id, correlation_id = receipt["run_id"], receipt["correlation_id"]
        assert run_id, "Receipt has no run ID"
        assert correlation_id == result["correlation_id"]
        assert document.get("run_id", run_id) == run_id
        assert receipt["status"] == "success"
        assert result["status"] == "completed"
        assert result["quality_gate_passed"] is True
        assert result["model_name"] == os.environ["ONEX_PROMPT_PROOF_MODEL"]
        assert result["response"], "No delegated answer"
        metrics = result["metrics"]
        assert metrics["input_tokens"] > 0, "Missing measured input usage"
        assert metrics["output_tokens"] > 0, "Missing measured output usage"
        assert any(
            attempt.get("usage_source") == "measured" for attempt in result["attempts"]
        ), "No measured provider usage"
        with sqlite3.connect(
            database_path.resolve().as_uri() + "?mode=ro", uri=True
        ) as database:
            database.row_factory = sqlite3.Row
            rows = database.execute(
                "SELECT correlation_id, trace_id, model_name, quality_gate_passed, "
                "tokens_input, tokens_output FROM delegation_events "
                "WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchall()
        assert len(rows) == 1, "Expected exactly one correlated delegation event"
        row = rows[0]
        assert row["trace_id"] == result["trace_id"]
        assert row["model_name"] == result["model_name"]
        assert row["quality_gate_passed"] == 1
        assert row["tokens_input"] == metrics["input_tokens"]
        assert row["tokens_output"] == metrics["output_tokens"]
        capture = json.loads(native_path.read_text())
        native = (
            capture.get("result", capture)
            if isinstance(capture.get("result"), dict)
            else capture
        )
        assert native["subtype"] == "success"
        assert native["is_error"] is False
        # A native mod produces an assistant turn without provider inference.
        # Count actual requests with a deliberately fired instrument control;
        # num_turns counts that assistant turn, not network calls.
        assert capture["instrument_control_passed"] is True
        assert capture["native_provider_requests"] == [], "Native Claude was called"
        assert native["duration_api_ms"] == 0
        assert native["modelUsage"] == {}
        assert native["total_cost_usd"] == 0, "Native Claude incurred cost"
        assert "blocked by hook" not in native["result"], "Answer is still a warning"
        assert capture["fallback_received_assistant_history"] is True
        assert len(capture["fallback_provider_requests"]) == 1
        assert capture["off_verified"] is True
        assert run_id in native["result"]
        assert result["model_name"] in native["result"]
        assert result["response"] in native["result"]
        evidence.update(
            status="passed",
            run_id=run_id,
            correlation_id=correlation_id,
            model=result["model_name"],
            input_tokens=metrics["input_tokens"],
            output_tokens=metrics["output_tokens"],
            receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            native_capture_sha256=hashlib.sha256(native_path.read_bytes()).hexdigest(),
            native_turns=native["num_turns"],
            native_provider_requests=0,
            fallback_provider_requests=1,
            database_read_only=True,
        )
    finally:
        artifact.write_text(json.dumps(evidence, indent=2))
