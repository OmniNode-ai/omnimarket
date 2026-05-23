# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerKBADRPublisher (OMN-11808)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_kb_adr_publisher.handlers.handler_kb_adr_publisher import (
    HandlerKBADRPublisher,
    _load_decisions,
)
from omnimarket.nodes.node_kb_adr_publisher.models.model_publish_request import (
    ModelKBADRPublishRequest,
)
from omnimarket.nodes.node_kb_adr_publisher.models.model_publish_result import (
    ModelKBADRPublishResult,
)


def _make_decision(model_id: str = "qwen3-coder-local") -> dict[str, Any]:
    return {
        "status": "Proposed",
        "date": "2026-05-23T10:00:00+00:00",
        "title": f"Use Pydantic for wire DTOs ({model_id})",
        "context": "Inconsistent DTO definitions across repos.",
        "decision": "All wire DTOs must be Pydantic BaseModel subclasses.",
        "consequences": "Easier validation; heavier import overhead.",
        "alternatives_considered": ["dataclasses", "TypedDict"],
        "supersedes": [],
        "source_evidence": ["seg-abc123"],
        "extraction_metadata": {
            "model_id": model_id,
            "confidence": 0.87,
            "pipeline_version": "1.0.0",
            "prompt_template_id": "adr-extraction-v3",
            "prompt_template_version": "3.0.1",
            "canary_run_id": "canary-2026-05-23-001",
            "extracted_at": "2026-05-23T10:00:00+00:00",
        },
    }


@pytest.fixture
def canary_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "canary-2026-05-23-001"
    run_dir.mkdir(parents=True)
    return run_dir


@pytest.fixture
def decisions_file(canary_run_dir: Path) -> Path:
    decisions = [
        _make_decision("qwen3-coder-local"),
        _make_decision("qwen3-coder-local"),
        _make_decision("deepseek-r1-local"),
    ]
    f = canary_run_dir / "extracted_decisions.json"
    f.write_text(json.dumps(decisions), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# _load_decisions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_decisions_filters_by_model_key(decisions_file: Path) -> None:
    results = _load_decisions(decisions_file, "qwen3-coder-local")
    assert len(results) == 2
    for r in results:
        assert r["extraction_metadata"]["model_id"] == "qwen3-coder-local"  # type: ignore[index]


@pytest.mark.unit
def test_load_decisions_returns_empty_for_unknown_model(decisions_file: Path) -> None:
    assert _load_decisions(decisions_file, "nonexistent-model") == []


@pytest.mark.unit
def test_load_decisions_returns_other_model(decisions_file: Path) -> None:
    assert len(_load_decisions(decisions_file, "deepseek-r1-local")) == 1


# ---------------------------------------------------------------------------
# HandlerKBADRPublisher — missing file
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_handle_returns_failure_when_decisions_file_missing(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "missing-run"
    run_dir.mkdir()
    request = ModelKBADRPublishRequest(
        canary_run_dir=str(run_dir),
        model_key="qwen3-coder-local",
    )
    result = await HandlerKBADRPublisher().handle(request=request)
    assert isinstance(result, ModelKBADRPublishResult)
    assert result.success is False
    assert result.error is not None
    assert "extracted_decisions.json" in result.error


@pytest.mark.unit
async def test_handle_returns_failure_when_no_matching_decisions(
    canary_run_dir: Path, decisions_file: Path
) -> None:
    request = ModelKBADRPublishRequest(
        canary_run_dir=str(canary_run_dir),
        model_key="nonexistent-model",
    )
    result = await HandlerKBADRPublisher().handle(request=request)
    assert result.success is False
    assert result.error is not None


# ---------------------------------------------------------------------------
# HandlerKBADRPublisher — dry_run
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_dry_run_succeeds_with_valid_decisions(
    canary_run_dir: Path, decisions_file: Path
) -> None:
    request = ModelKBADRPublishRequest(
        canary_run_dir=str(canary_run_dir),
        model_key="qwen3-coder-local",
        dry_run=True,
    )
    result = await HandlerKBADRPublisher().handle(request=request)
    assert result.success is True
    assert result.adr_count == 2
    assert result.pr_url is None
    assert result.branch is None


@pytest.mark.unit
async def test_dry_run_does_not_call_subprocess(
    canary_run_dir: Path, decisions_file: Path
) -> None:
    request = ModelKBADRPublishRequest(
        canary_run_dir=str(canary_run_dir),
        model_key="qwen3-coder-local",
        dry_run=True,
    )
    with patch("subprocess.run") as mock_run:
        await HandlerKBADRPublisher().handle(request=request)
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# HandlerKBADRPublisher — live path (subprocess mocked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_live_run_returns_success_with_pr_url(
    canary_run_dir: Path, decisions_file: Path, tmp_path: Path
) -> None:
    def fake_subprocess(cmd: list[str], **kwargs: Any) -> MagicMock:
        if "clone" in cmd:
            clone_dir = Path(cmd[-1])
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / ".git").mkdir(exist_ok=True)
        m = MagicMock()
        m.stdout = "https://github.com/OmniNode-ai/knowledge-base/pull/42\n"
        m.returncode = 0
        return m

    with (
        patch("subprocess.run", side_effect=fake_subprocess),
        patch(
            "omnimarket.nodes.node_kb_adr_publisher.handlers.handler_kb_adr_publisher.render_adr_to_kb"
        ) as mock_render,
    ):
        mock_render.return_value = MagicMock(
            adr_path=tmp_path / "fake.md",
            evidence_path=tmp_path / "fake-evidence.json",
        )
        request = ModelKBADRPublishRequest(
            canary_run_dir=str(canary_run_dir),
            model_key="qwen3-coder-local",
        )
        result = await HandlerKBADRPublisher().handle(request=request)

    assert result.success is True
    assert result.adr_count == 2
    assert result.pr_url == "https://github.com/OmniNode-ai/knowledge-base/pull/42"
    assert result.branch is not None
    assert "canary-2026-05-23-001" in result.branch


@pytest.mark.unit
async def test_live_run_calls_gh_clone(
    canary_run_dir: Path, decisions_file: Path, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_subprocess(cmd: list[str], **kwargs: Any) -> MagicMock:
        calls.append(list(cmd))
        if "clone" in cmd:
            clone_dir = Path(cmd[-1])
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / ".git").mkdir(exist_ok=True)
        m = MagicMock()
        m.stdout = "https://github.com/OmniNode-ai/knowledge-base/pull/99\n"
        m.returncode = 0
        return m

    with (
        patch("subprocess.run", side_effect=fake_subprocess),
        patch(
            "omnimarket.nodes.node_kb_adr_publisher.handlers.handler_kb_adr_publisher.render_adr_to_kb"
        ) as mock_render,
    ):
        mock_render.return_value = MagicMock(
            adr_path=tmp_path / "fake.md",
            evidence_path=tmp_path / "fake-evidence.json",
        )
        request = ModelKBADRPublishRequest(
            canary_run_dir=str(canary_run_dir),
            model_key="qwen3-coder-local",
        )
        await HandlerKBADRPublisher().handle(request=request)

    clone_calls = [c for c in calls if "clone" in c]
    pr_calls = [c for c in calls if "pr" in c and "create" in c]
    assert len(clone_calls) == 1
    assert len(pr_calls) == 1
