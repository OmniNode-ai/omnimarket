# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15446: current gh schema and canonical repository identity regressions."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.events.verification import ModelVerificationReceiptRequest
from omnimarket.nodes.node_verification_receipt_generator.handlers.handler_verification_receipt import (
    GhClient,
    HandlerVerificationReceiptGenerator,
)

_HANDLER_MODULE = (
    "omnimarket.nodes.node_verification_receipt_generator.handlers."
    "handler_verification_receipt"
)
_REPO = "OmniNode-ai/onex_change_control"


def _request() -> ModelVerificationReceiptRequest:
    return ModelVerificationReceiptRequest(
        task_id="OMN-15446",
        claim="the hosted checks are classified from current gh fields",
        repo=_REPO,
        pr_number=5522,
        verify_ci=True,
        verify_tests=False,
    )


def _run_real_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int,
    stdout: str,
    stderr: str = "",
) -> tuple[Any, list[str]]:
    captured: list[str] = []

    def _run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured.extend(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(f"{_HANDLER_MODULE}.subprocess.run", _run)
    receipt = HandlerVerificationReceiptGenerator(
        gh_client=GhClient("test-token")
    ).handle(_request())
    return receipt, captured


@pytest.mark.unit
def test_documented_full_repo_slug_round_trips_to_supported_gh_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, argv = _run_real_client(
        monkeypatch,
        returncode=0,
        stdout=json.dumps([{"name": "verify", "state": "SUCCESS", "bucket": "pass"}]),
    )

    assert argv[argv.index("--repo") + 1] == _REPO
    fields = argv[argv.index("--json") + 1].split(",")
    assert fields == ["name", "state", "bucket"]
    assert "conclusion" not in fields
    assert receipt.overall_pass is True
    assert receipt.checks[0].details["verify"] == "pass:SUCCESS"


@pytest.mark.unit
@pytest.mark.parametrize(
    "repo",
    [
        "omnimarket",
        "OmniNode-ai/",
        "/omnimarket",
        "OmniNode-ai/OmniNode-ai/omnimarket",
        " OmniNode-ai/omnimarket",
        "OmniNode-ai/omnimarket ",
    ],
)
def test_request_rejects_noncanonical_repository_identity(repo: str) -> None:
    with pytest.raises(ValidationError, match="repo"):
        ModelVerificationReceiptRequest(
            task_id="OMN-15446",
            claim="reject ambiguous repository identity",
            repo=repo,
            pr_number=1,
            verify_ci=True,
            verify_tests=False,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rows", "expected_pass", "summary_fragment"),
    [
        (
            [
                {"name": "lint", "state": "SUCCESS", "bucket": "pass"},
                {"name": "docs", "state": "SKIPPED", "bucket": "skipping"},
            ],
            True,
            "All 2 CI checks passed",
        ),
        (
            [{"name": "tests", "state": "FAILURE", "bucket": "fail"}],
            False,
            "tests (fail)",
        ),
        (
            [{"name": "tests", "state": "PENDING", "bucket": "pending"}],
            False,
            "tests (pending)",
        ),
        (
            [{"name": "tests", "state": "CANCELLED", "bucket": "cancel"}],
            False,
            "tests (cancel)",
        ),
    ],
)
def test_supported_buckets_classify_deterministically(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, str]],
    expected_pass: bool,
    summary_fragment: str,
) -> None:
    receipt, _ = _run_real_client(
        monkeypatch,
        returncode=0,
        stdout=json.dumps(rows),
    )

    ci = receipt.checks[0]
    assert ci.passed is expected_pass
    assert summary_fragment in ci.summary


@pytest.mark.unit
@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "summary_fragment"),
    [
        (0, "[]", "", "No CI check data"),
        (0, "not-json", "", "malformed gh pr checks JSON"),
        (1, "", "authentication failed", "gh pr checks exit 1"),
        (-9, "", "", "gh pr checks exit -9"),
        (
            0,
            json.dumps([{"name": "tests", "state": "MYSTERY", "bucket": "new"}]),
            "",
            "invalid gh pr checks row",
        ),
        (
            0,
            json.dumps([{"name": "tests", "state": "MYSTERY", "bucket": "pending"}]),
            "",
            "invalid gh pr checks row",
        ),
        (
            0,
            json.dumps([{"name": "tests", "state": "FAILURE", "bucket": "pass"}]),
            "",
            "invalid gh pr checks row",
        ),
    ],
)
def test_empty_malformed_unknown_and_command_error_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    stderr: str,
    summary_fragment: str,
) -> None:
    receipt, _ = _run_real_client(
        monkeypatch,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )

    ci = receipt.checks[0]
    assert ci.passed is False
    assert summary_fragment in ci.summary
    if stdout != "[]":
        assert summary_fragment in ci.details["query_error"]


@pytest.mark.unit
def test_timeout_is_a_typed_fail_closed_query_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _timeout(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["gh", "pr", "checks"], timeout=30)

    monkeypatch.setattr(f"{_HANDLER_MODULE}.subprocess.run", _timeout)
    receipt = HandlerVerificationReceiptGenerator(
        gh_client=GhClient("test-token")
    ).handle(_request())

    ci = receipt.checks[0]
    assert ci.passed is False
    assert "timed out" in ci.summary
    assert "timed out" in ci.details["query_error"]
