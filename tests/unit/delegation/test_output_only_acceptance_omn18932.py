# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic D1 acceptance controls over the content returned to the caller.

These controls distinguish provider-side preamble telemetry from the actual
terminal content: clean terminal content may pass, but text that needs manual
extraction from the terminal content must fail.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from omnimarket.delegation import response_contract_conformance_runner as runner

_ARTIFACT = "## Result\n\nThe requested deliverable."
_PROMPT = "Return exactly the requested Markdown artifact, with no other text."


def _manifest() -> dict[str, object]:
    return {
        "manifest_id": "omn18932-synthetic-output-only-control",
        "local_only": True,
        "contracts": [
            {
                "contract_id": "synthetic-exact-markdown-artifact",
                "task_type": "document",
                "expected_model": "Qwen3.8-27B",
                "prompt": _PROMPT,
                "output_shape": "markdown",
                "returned_content_pattern": re.escape(_ARTIFACT),
                "minimum_pass_rate": 1.0,
                "response_contract": {"x-omninode-output-shape": "markdown"},
                "markers": [],
                "trials": 1,
            }
        ],
    }


def _run_terminal_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returned_content: str,
    *,
    raw_preamble_chars: int,
    cleaned_alias: str | None = None,
) -> dict[str, object]:
    manifest = _manifest()
    contract = manifest["contracts"][0]
    assert isinstance(contract, dict)
    response_contract = contract["response_contract"]
    assert isinstance(response_contract, dict)
    resolved = runner.resolve_task_class_deliverable_contract(
        "document", response_contract
    )
    terminal: dict[str, object] = {
        "run_id": "synthetic-omn18932-run",
        "content": returned_content,
        "preamble_chars": raw_preamble_chars,
        "quality_passed": True,
        "cost_tier_name": "local",
        "model_used": "Qwen3.8-27B",
        "response_contract_evidence": {
            "conveyed": True,
            "validated": True,
            "output_shape": "markdown",
            "contract_sha256": runner.canonical_deliverable_contract_sha256(resolved),
            "channel": "messages[0].content",
        },
        "budget_evidence": {
            "requested_timeout_seconds": 30,
            "task_class_timeout_ceiling_seconds": 60,
            "execution_timeout_seconds": 30,
            "terminal_delivery_margin_seconds": 5,
        },
    }
    if cleaned_alias is not None:
        # Deliberately non-authoritative synthetic field: the runner must judge
        # the caller-returned `content`, never a separately cleaned candidate.
        terminal["cleaned_content"] = cleaned_alias

    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    def completed_process(command: list[str], **_: object) -> CompletedProcess[str]:
        return CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps({"terminal": terminal}),
            stderr="",
        )

    monkeypatch.setattr(runner.subprocess, "run", completed_process)
    return runner.run_live_manifest(manifest, timeout_seconds=30)


@pytest.mark.unit
def test_k5_clean_returned_artifact_passes_with_provider_preamble_telemetry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Provider trace metadata does not replace the clean caller answer."""
    receipt = _run_terminal_content(
        monkeypatch,
        tmp_path,
        _ARTIFACT,
        raw_preamble_chars=len("provider-side preamble"),
    )

    assert receipt["passed"] is True
    trial = receipt["contracts"][0]["trials"][0]
    assert trial["raw_preamble_chars"] > 0
    assert trial["returned_content_valid"] is True
    assert trial["passed"] is True


@pytest.mark.parametrize(
    "returned_content",
    [
        "Planning notes before the artifact.\n\n" + _ARTIFACT,
        _ARTIFACT + "\n\nI hope this helps.",
    ],
    ids=("preamble", "trailing-self-review"),
)
@pytest.mark.unit
def test_k5_manual_extraction_cannot_rescue_returned_terminal_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returned_content: str,
) -> None:
    """The exact caller-returned bytes fail even beside a clean alias field."""
    receipt = _run_terminal_content(
        monkeypatch,
        tmp_path,
        returned_content,
        raw_preamble_chars=0,
        cleaned_alias=_ARTIFACT,
    )

    assert receipt["passed"] is False
    trial = receipt["contracts"][0]["trials"][0]
    assert trial["returned_content_valid"] is False
    assert trial["passed"] is False
