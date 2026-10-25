# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SCM CLI commands must host their canonical node on the in-memory bus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.handlers.scm.scm_cli import scm_group

pytestmark = pytest.mark.unit


def test_pr_create_dry_run_returns_a_real_terminal_without_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BIFROST_BASE_CONTRACT_PATH", raising=False)
    result = CliRunner().invoke(
        scm_group,
        [
            "pr-create",
            "OmniNode-ai/omnimarket",
            "--head",
            "omn-21279-scm-inprocess-terminal",
            "--base",
            "dev",
            "--title",
            "In-process terminal probe",
            "--body",
            "Dry run",
            "--lane",
            "scm-inprocess-test",
            "--bus",
            "inmemory",
            "--timeout-s",
            "0.1",
            "--dry-run",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    answer = json.loads(result.output)
    assert answer["outcome"] == "completed"
    assert answer["result"]["mode"] == "dry_run"
    assert answer["bus"]["terminal_topic"].endswith("github-repo-write-completed.v1")
    assert answer["bus"]["terminal_envelope_id"]
    assert answer["bus"]["correlation_id"] == answer["result"]["correlation_id"]


def test_pr_close_without_a_claim_returns_the_failure_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_PR_CLAIMS_DIR", str(tmp_path))
    result = CliRunner().invoke(
        scm_group,
        [
            "pr-close",
            "OmniNode-ai/omnimarket#4000",
            "--head-sha",
            "a" * 40,
            "--run",
            "inprocess-test",
            "--lane",
            "scm-inprocess-test",
            "--bus",
            "inmemory",
            "--timeout-s",
            "0.1",
            "--dry-run",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 1, result.output
    answer = json.loads(result.stdout)
    assert answer["outcome"] == "failed"
    assert answer["result"]["reason"] == "claim_not_held"
    assert answer["bus"]["terminal_topic"].endswith("github-repo-write-failed.v1")
    assert answer["bus"]["terminal_envelope_id"]


def test_pr_threads_uses_the_gateway_handler_and_returns_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers import (
        handler_github_repo_gateway as gateway,
    )
    from tests.nodes.node_github_repo_gateway_effect.test_github_repo_gateway_review_threads import (
        _pages,
        _transport,
    )

    transport = _transport(_pages())

    async def token() -> str:
        return "recorded-transport-token"

    monkeypatch.setattr(gateway, "resolve_github_token_async", token)
    monkeypatch.setattr(gateway, "RealGitHubReadTransport", lambda _token: transport)
    result = CliRunner().invoke(
        scm_group,
        [
            "pr-threads",
            "OmniNode-ai/omnimarket#4000",
            "--bus",
            "inmemory",
            "--timeout-s",
            "0.1",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout)
    assert answer["outcome"] == "completed"
    assert answer["result"]["operation"] == "pr_review_threads"
    assert answer["result"]["total_count"] == 4
    assert len(answer["result"]["threads"]) == 3
    assert answer["bus"]["terminal_envelope_id"]
    assert transport.exchanges == []


def test_gateway_boundary_failure_is_reported_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers import (
        handler_github_repo_gateway as gateway,
    )

    async def token() -> str:
        raise RuntimeError("credential unavailable")

    monkeypatch.setattr(gateway, "resolve_github_token_async", token)
    result = CliRunner().invoke(
        scm_group,
        [
            "pr-threads",
            "OmniNode-ai/omnimarket#4000",
            "--bus",
            "inmemory",
            "--timeout-s",
            "0.1",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 1, result.output
    answer = json.loads(result.stdout)
    assert answer["outcome"] == "failed"
    assert answer["result"]["status"] == "failed"
    assert answer["bus"]["terminal_envelope_id"]
