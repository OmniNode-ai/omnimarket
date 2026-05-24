# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerKBRepoWiseIndex (OMN-11914)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_kb_repowise_index_effect.handlers.handler_kb_repowise_index import (
    HandlerKBRepoWiseIndex,
    _parse_entry_count,
)
from omnimarket.nodes.node_kb_repowise_index_effect.models.model_index_request import (
    ModelKBRepoIndexRequest,
)
from omnimarket.nodes.node_kb_repowise_index_effect.models.model_index_result import (
    ModelKBRepoIndexResult,
)

# ---------------------------------------------------------------------------
# _parse_entry_count
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_entry_count_extracts_last_integer() -> None:
    output = "Indexed 42 documents\n"
    assert _parse_entry_count(output) == 42


@pytest.mark.unit
def test_parse_entry_count_returns_zero_on_empty() -> None:
    assert _parse_entry_count("") == 0


@pytest.mark.unit
def test_parse_entry_count_returns_zero_when_no_integers() -> None:
    assert _parse_entry_count("done\nok\n") == 0


@pytest.mark.unit
def test_parse_entry_count_picks_last_line_integer() -> None:
    output = "Starting index\nProcessing files\nTotal entries: 17\n"
    assert _parse_entry_count(output) == 17


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_dry_run_returns_success_without_subprocess() -> None:
    request = ModelKBRepoIndexRequest(dry_run=True)
    with patch("subprocess.run") as mock_run:
        result = await HandlerKBRepoWiseIndex().handle(request=request)
    mock_run.assert_not_called()
    assert isinstance(result, ModelKBRepoIndexResult)
    assert result.success is True
    assert result.commit_sha is None
    assert result.entry_count == 0
    assert result.error is None


# ---------------------------------------------------------------------------
# clone failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_clone_failure_returns_error_result() -> None:
    import subprocess

    request = ModelKBRepoIndexRequest(kb_repo="OmniNode-ai/knowledge-base")

    def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
        if "clone" in cmd:
            raise subprocess.CalledProcessError(
                returncode=1, cmd=cmd, stderr="authentication failed"
            )
        return MagicMock(stdout="", returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = await HandlerKBRepoWiseIndex().handle(request=request)

    assert result.success is False
    assert result.error is not None
    assert "Clone failed" in result.error


# ---------------------------------------------------------------------------
# repowise index failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_repowise_failure_returns_error_with_commit_sha(tmp_path: Any) -> None:
    import subprocess

    request = ModelKBRepoIndexRequest(kb_repo="OmniNode-ai/knowledge-base")

    def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
        if "clone" in cmd:
            clone_dir = cmd[-1]
            import os

            os.makedirs(clone_dir, exist_ok=True)
            m = MagicMock()
            m.stdout = ""
            m.returncode = 0
            return m
        if "rev-parse" in cmd:
            m = MagicMock()
            m.stdout = "abc1234def5678\n"
            m.returncode = 0
            return m
        if "index" in cmd:
            raise subprocess.CalledProcessError(
                returncode=2, cmd=cmd, stderr="index engine unavailable"
            )
        return MagicMock(stdout="", returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = await HandlerKBRepoWiseIndex().handle(request=request)

    assert result.success is False
    assert result.error is not None
    assert "Repowise index failed" in result.error


# ---------------------------------------------------------------------------
# happy path — subprocess mocked
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_successful_index_returns_commit_sha_and_entry_count() -> None:
    request = ModelKBRepoIndexRequest(kb_repo="OmniNode-ai/knowledge-base")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
        calls.append(list(cmd))
        m = MagicMock()
        m.returncode = 0
        if "clone" in cmd:
            clone_dir = cmd[-1]
            import os

            os.makedirs(clone_dir, exist_ok=True)
            m.stdout = ""
        elif "rev-parse" in cmd:
            m.stdout = "deadbeef1234567890abcdef\n"
        elif "index" in cmd:
            m.stdout = "Indexed 99 documents\n"
        else:
            m.stdout = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        result = await HandlerKBRepoWiseIndex().handle(request=request)

    assert result.success is True
    assert result.commit_sha == "deadbeef1234567890abcdef"
    assert result.entry_count == 99
    assert result.error is None


@pytest.mark.unit
async def test_successful_index_calls_gh_clone_and_repowise() -> None:
    request = ModelKBRepoIndexRequest(kb_repo="OmniNode-ai/knowledge-base")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
        calls.append(list(cmd))
        m = MagicMock()
        m.returncode = 0
        if "clone" in cmd:
            clone_dir = cmd[-1]
            import os

            os.makedirs(clone_dir, exist_ok=True)
            m.stdout = ""
        elif "rev-parse" in cmd:
            m.stdout = "cafebabe\n"
        elif "index" in cmd:
            m.stdout = "Indexed 5 entries\n"
        else:
            m.stdout = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        await HandlerKBRepoWiseIndex().handle(request=request)

    clone_calls = [c for c in calls if "clone" in c]
    index_calls = [c for c in calls if "index" in c]
    assert len(clone_calls) == 1
    assert "OmniNode-ai/knowledge-base" in clone_calls[0]
    assert len(index_calls) == 1
    assert "repowise" in index_calls[0]
