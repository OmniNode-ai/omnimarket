# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Explicit completion requires actual operator/model artifacts, never all-skips."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.e2e


def test_required_customer_evidence_close_probe(tmp_path: Path) -> None:
    """An explicit completion probe must fail rather than quietly skip evidence."""
    if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") != "1":
        pytest.skip("Explicit close probe requires ONEX_E2E_REQUIRE_COMPLETE=1")
    names = (
        "ONEX_TEST_CLI",
        "ONEX_TEST_WHEEL",
        "ONEX_TEST_CORE_WHEEL",
        "ONEX_TEST_CLAUDE_WHEEL",
        "ONEX_TEST_REAL_MODEL_RECEIPT",
        "ONEX_TEST_REAL_MODEL_URL",
        "ONEX_TEST_REAL_MODEL_ID",
    )
    missing = [name for name in names if not os.environ.get(name)]
    evidence: dict[str, Any] = {
        "status": "failed",
        "missing_inputs": missing,
        "real_model_proof": False,
        "fixture_is_real_model_proof": False,
    }
    artifact = tmp_path / "required-customer-evidence.json"
    try:
        assert not missing, "Required completion evidence missing: " + ", ".join(
            missing
        )
        for name in names[:5]:
            assert Path(os.environ[name]).is_file(), (
                f"Required artifact missing: {name}"
            )
        receipt = json.loads(
            Path(os.environ["ONEX_TEST_REAL_MODEL_RECEIPT"]).read_text()
        )
        assert receipt.get("status") == "success", "Real model receipt did not succeed"
        assert receipt.get("model")
        assert receipt.get("endpoint")
        assert receipt.get("run_id")
        assert receipt["model"] == os.environ["ONEX_TEST_REAL_MODEL_ID"]
        assert receipt["endpoint"] == os.environ["ONEX_TEST_REAL_MODEL_URL"], (
            "Receipt must match the operator-supplied model endpoint"
        )
        terminal = receipt["receipt"]
        assert terminal["status"] == "success"
        assert terminal["run_id"] == receipt["run_id"]
        assert terminal["correlation_id"] == receipt["correlation_id"]
        assert terminal["result"]["model_name"] == receipt["model"]
        assert terminal["result"]["response"]
        assert terminal["artifact_refs"], "Canonical capture artifacts are required"
        identity = terminal["runtime_identity"]
        assert (
            Path(identity["interpreter"]).resolve()
            == (Path(os.environ["ONEX_TEST_CLI"]).parent / "python").resolve()
        )
        assert identity["packages"]["omnimarket"]["version"]
        evidence["receipt_sha256"] = hashlib.sha256(
            Path(os.environ["ONEX_TEST_REAL_MODEL_RECEIPT"]).read_bytes()
        ).hexdigest()
        evidence.update(
            status="passed",
            real_model_proof=True,
            run_id=receipt["run_id"],
            model=receipt["model"],
            endpoint=receipt["endpoint"],
        )
    finally:
        artifact.write_text(json.dumps(evidence, indent=2))
