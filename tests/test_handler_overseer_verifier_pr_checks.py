# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the pr_checks_live dimension of HandlerOverseerVerifier.

Mocks ``subprocess.run`` so we never actually shell out to ``gh``. Covers:
- no claimed PRs → check skipped, verdict PASS
- all claimed PRs green → verdict PASS, pr_checks_live passed
- one green + one red → verdict FAIL, red PR surfaces in failure reasons
- gh timeout → verdict FAIL with descriptive message
- gh non-zero exit → verdict FAIL surfacing exit code + stderr
- malformed JSON → verdict FAIL with parse-error message

Related:
    - OMN-9273: Wire gh pr checks against agent self-reports
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier import (
    HandlerOverseerVerifier,
)
from omnimarket.nodes.node_overseer_verifier.models.model_claimed_pr import (
    ModelClaimedPr,
)
from omnimarket.nodes.node_overseer_verifier.models.model_verifier_request import (
    ModelVerifierRequest,
)


def _make_request(
    claimed_prs: list[ModelClaimedPr] | None = None,
    **overrides: object,
) -> ModelVerifierRequest:
    """Build a valid ModelVerifierRequest with all non-PR checks passing."""
    defaults: dict[str, object] = {
        "task_id": "task-gh-123",
        "status": "running",
        "domain": "build",
        "node_id": "node_build_loop",
        "runner_id": "runner-001",
        "attempt": 1,
        "payload": {"key": "value"},
        "error": None,
        "confidence": 0.9,
        "cost_so_far": 0.05,
        "allowed_actions": ["dispatch", "complete"],
        "declared_invariants": [],
        "schema_version": "1.0",
        "claimed_prs": claimed_prs or [],
    }
    defaults.update(overrides)
    return ModelVerifierRequest(**defaults)  # type: ignore[arg-type]


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess sufficient for handler use."""

    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _green_rows() -> list[dict[str, Any]]:
    return [
        {
            "bucket": "pass",
            "state": "SUCCESS",
            "conclusion": "success",
            "name": "test",
            "completedAt": "2026-04-19T12:00:00Z",
        },
        {
            "bucket": "pass",
            "state": "SUCCESS",
            "conclusion": "success",
            "name": "lint",
            "completedAt": "2026-04-19T12:01:00Z",
        },
    ]


def _red_rows() -> list[dict[str, Any]]:
    return [
        {
            "bucket": "pass",
            "state": "SUCCESS",
            "conclusion": "success",
            "name": "lint",
            "completedAt": "2026-04-19T12:01:00Z",
        },
        {
            "bucket": "fail",
            "state": "COMPLETED",
            "conclusion": "failure",
            "name": "test",
            "completedAt": "2026-04-19T12:05:00Z",
        },
    ]


@pytest.mark.unit
def test_pr_checks_live_skipped_when_no_claimed_prs() -> None:
    """When no PRs are claimed, subprocess is never invoked and the check passes."""
    handler = HandlerOverseerVerifier()
    with patch(
        "omnimarket.nodes.node_overseer_verifier.handlers."
        "handler_overseer_verifier.subprocess.run"
    ) as mock_run:
        result = handler.verify(_make_request(claimed_prs=[]))

    mock_run.assert_not_called()
    assert result["verdict"] == "PASS"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["pr_checks_live"]["passed"] is True


@pytest.mark.unit
def test_pr_checks_live_passes_when_all_prs_green() -> None:
    """Two claimed PRs, both returning all-green rows → verdict PASS."""
    handler = HandlerOverseerVerifier()
    claimed = [
        ModelClaimedPr(pr_number=101, repo="OmniNode-ai/omnimarket"),
        ModelClaimedPr(pr_number=202, repo="OmniNode-ai/omniclaude"),
    ]
    green_stdout = json.dumps(_green_rows())
    with patch(
        "omnimarket.nodes.node_overseer_verifier.handlers."
        "handler_overseer_verifier.subprocess.run",
        return_value=_FakeCompleted(stdout=green_stdout, returncode=0),
    ) as mock_run:
        result = handler.verify(_make_request(claimed_prs=claimed))

    assert mock_run.call_count == 2
    # Confirm the expected CLI shape was invoked
    first_cmd = mock_run.call_args_list[0].args[0]
    assert first_cmd[:3] == ["gh", "pr", "checks"]
    assert "--json" in first_cmd
    assert "bucket,state,conclusion,name,completedAt" in first_cmd

    assert result["verdict"] == "PASS"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["pr_checks_live"]["passed"] is True
    assert "verified green" in checks["pr_checks_live"]["message"]


@pytest.mark.unit
def test_pr_checks_live_fails_when_one_pr_has_red_check() -> None:
    """One green + one red PR → verdict FAIL, red PR + failing check named."""
    handler = HandlerOverseerVerifier()
    claimed = [
        ModelClaimedPr(pr_number=101, repo="OmniNode-ai/omnimarket"),
        ModelClaimedPr(pr_number=202, repo="OmniNode-ai/omniclaude"),
    ]
    green_stdout = json.dumps(_green_rows())
    red_stdout = json.dumps(_red_rows())

    def _fake_run(cmd: list[str], **_: object) -> _FakeCompleted:
        pr_number = cmd[3]
        if pr_number == "101":
            return _FakeCompleted(stdout=green_stdout, returncode=0)
        return _FakeCompleted(stdout=red_stdout, returncode=0)

    with patch(
        "omnimarket.nodes.node_overseer_verifier.handlers."
        "handler_overseer_verifier.subprocess.run",
        side_effect=_fake_run,
    ):
        result = handler.verify(_make_request(claimed_prs=claimed))

    assert result["verdict"] == "FAIL"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["pr_checks_live"]["passed"] is False
    msg = str(checks["pr_checks_live"]["message"])
    assert "OmniNode-ai/omniclaude#202" in msg
    assert "test" in msg  # the failing check name
    assert "OmniNode-ai/omnimarket#101" not in msg
    # Summary should reference pr_checks_live
    assert "pr_checks_live" in str(result["summary"])


@pytest.mark.unit
def test_pr_checks_live_fails_on_subprocess_timeout() -> None:
    """A subprocess timeout is reported as a failure, not an uncaught exception."""
    handler = HandlerOverseerVerifier()
    claimed = [ModelClaimedPr(pr_number=1, repo="OmniNode-ai/omnimarket")]

    with patch(
        "omnimarket.nodes.node_overseer_verifier.handlers."
        "handler_overseer_verifier.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
    ):
        result = handler.verify(_make_request(claimed_prs=claimed))

    assert result["verdict"] == "FAIL"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["pr_checks_live"]["passed"] is False
    assert "timeout" in str(checks["pr_checks_live"]["message"]).lower()


@pytest.mark.unit
def test_pr_checks_live_fails_on_gh_nonzero_exit() -> None:
    """Non-zero exit from gh surfaces exit code + stderr in the failure message."""
    handler = HandlerOverseerVerifier()
    claimed = [ModelClaimedPr(pr_number=1, repo="OmniNode-ai/omnimarket")]

    with patch(
        "omnimarket.nodes.node_overseer_verifier.handlers."
        "handler_overseer_verifier.subprocess.run",
        return_value=_FakeCompleted(
            stdout="", returncode=1, stderr="gh: not authenticated"
        ),
    ):
        result = handler.verify(_make_request(claimed_prs=claimed))

    assert result["verdict"] == "FAIL"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["pr_checks_live"]["passed"] is False
    msg = str(checks["pr_checks_live"]["message"])
    assert "gh exit 1" in msg
    assert "not authenticated" in msg


@pytest.mark.unit
def test_pr_checks_live_fails_on_malformed_json() -> None:
    """Invalid JSON from gh surfaces a parse-error in the failure message."""
    handler = HandlerOverseerVerifier()
    claimed = [ModelClaimedPr(pr_number=1, repo="OmniNode-ai/omnimarket")]

    with patch(
        "omnimarket.nodes.node_overseer_verifier.handlers."
        "handler_overseer_verifier.subprocess.run",
        return_value=_FakeCompleted(stdout="not-json", returncode=0),
    ):
        result = handler.verify(_make_request(claimed_prs=claimed))

    assert result["verdict"] == "FAIL"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["pr_checks_live"]["passed"] is False
    assert "JSON parse error" in str(checks["pr_checks_live"]["message"])


@pytest.mark.unit
def test_pr_checks_live_fails_on_non_list_json() -> None:
    """Top-level JSON object (not a list) is reported as a shape error, not a crash."""
    handler = HandlerOverseerVerifier()
    claimed = [ModelClaimedPr(pr_number=1, repo="OmniNode-ai/omnimarket")]

    with patch(
        "omnimarket.nodes.node_overseer_verifier.handlers."
        "handler_overseer_verifier.subprocess.run",
        return_value=_FakeCompleted(stdout='{"unexpected": "object"}', returncode=0),
    ):
        result = handler.verify(_make_request(claimed_prs=claimed))

    assert result["verdict"] == "FAIL"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["pr_checks_live"]["passed"] is False
    assert "JSON shape error" in str(checks["pr_checks_live"]["message"])
    assert "top-level list" in str(checks["pr_checks_live"]["message"])


@pytest.mark.unit
def test_pr_checks_live_fails_on_non_dict_list_items() -> None:
    """List items that aren't dicts are reported as a shape error, not a crash."""
    handler = HandlerOverseerVerifier()
    claimed = [ModelClaimedPr(pr_number=1, repo="OmniNode-ai/omnimarket")]

    with patch(
        "omnimarket.nodes.node_overseer_verifier.handlers."
        "handler_overseer_verifier.subprocess.run",
        return_value=_FakeCompleted(stdout='["string-row", 42]', returncode=0),
    ):
        result = handler.verify(_make_request(claimed_prs=claimed))

    assert result["verdict"] == "FAIL"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["pr_checks_live"]["passed"] is False
    assert "JSON shape error" in str(checks["pr_checks_live"]["message"])
    assert "list of objects" in str(checks["pr_checks_live"]["message"])


@pytest.mark.unit
def test_pr_checks_live_tolerates_non_string_check_names() -> None:
    """Rows with `name: null` or non-string `name` values don't crash sort/join."""
    handler = HandlerOverseerVerifier()
    claimed = [ModelClaimedPr(pr_number=1, repo="OmniNode-ai/omnimarket")]
    mixed_rows: list[dict[str, Any]] = [
        {"bucket": "fail", "state": "COMPLETED", "conclusion": "failure", "name": None},
        {"bucket": "fail", "state": "COMPLETED", "conclusion": "failure", "name": 42},
        {
            "bucket": "fail",
            "state": "COMPLETED",
            "conclusion": "failure",
            "name": "lint",
        },
    ]
    stdout = json.dumps(mixed_rows)

    with patch(
        "omnimarket.nodes.node_overseer_verifier.handlers."
        "handler_overseer_verifier.subprocess.run",
        return_value=_FakeCompleted(stdout=stdout, returncode=0),
    ):
        result = handler.verify(_make_request(claimed_prs=claimed))

    # Must not raise TypeError — verdict surfaces the failing checks normally.
    assert result["verdict"] == "FAIL"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["pr_checks_live"]["passed"] is False
    msg = str(checks["pr_checks_live"]["message"])
    assert "lint" in msg
    # None-name is replaced with "<unnamed>", int is coerced via str().
    assert "<unnamed>" in msg
    assert "42" in msg


@pytest.mark.unit
def test_pr_checks_live_fails_when_claimed_prs_exceed_cap() -> None:
    """claimed_prs beyond the hard cap fail fast without shelling out."""
    handler = HandlerOverseerVerifier()
    # 21 claimed PRs — exceeds the current 20-PR cap
    claimed = [
        ModelClaimedPr(pr_number=i, repo="OmniNode-ai/omnimarket") for i in range(1, 22)
    ]

    with patch(
        "omnimarket.nodes.node_overseer_verifier.handlers."
        "handler_overseer_verifier.subprocess.run",
    ) as mock_run:
        result = handler.verify(_make_request(claimed_prs=claimed))

    # Fail-fast: subprocess never invoked when the cap is exceeded
    mock_run.assert_not_called()
    assert result["verdict"] == "FAIL"
    checks = {c["name"]: c for c in result["checks"]}  # type: ignore[union-attr]
    assert checks["pr_checks_live"]["passed"] is False
    msg = str(checks["pr_checks_live"]["message"])
    assert "too many claimed PRs" in msg
    assert "21" in msg
    assert "20" in msg
