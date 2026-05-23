# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for publish_canary_adrs_to_kb script (OMN-11808)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure the scripts directory is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import publish_canary_adrs_to_kb as script

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    results = script._load_decisions(decisions_file, "qwen3-coder-local")
    assert len(results) == 2
    for r in results:
        assert r["extraction_metadata"]["model_id"] == "qwen3-coder-local"  # type: ignore[index]


@pytest.mark.unit
def test_load_decisions_returns_empty_for_unknown_model(decisions_file: Path) -> None:
    results = script._load_decisions(decisions_file, "nonexistent-model")
    assert results == []


@pytest.mark.unit
def test_load_decisions_returns_other_model(decisions_file: Path) -> None:
    results = script._load_decisions(decisions_file, "deepseek-r1-local")
    assert len(results) == 1


# ---------------------------------------------------------------------------
# _run — missing file
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_returns_1_when_decisions_file_missing(tmp_path: Path) -> None:
    run_dir = tmp_path / "missing-run"
    run_dir.mkdir()
    rc = script._run(
        decisions_file=run_dir / "extracted_decisions.json",
        canary_run_dir=run_dir,
        model_key="qwen3-coder-local",
        kb_repo="OmniNode-ai/knowledge-base",
        dry_run=False,
    )
    assert rc == 1


@pytest.mark.unit
def test_run_returns_1_when_no_matching_decisions(
    canary_run_dir: Path, decisions_file: Path
) -> None:
    rc = script._run(
        decisions_file=decisions_file,
        canary_run_dir=canary_run_dir,
        model_key="nonexistent-model",
        kb_repo="OmniNode-ai/knowledge-base",
        dry_run=False,
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# _run — dry-run mode (no side effects)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dry_run_succeeds_with_valid_decisions(
    canary_run_dir: Path, decisions_file: Path
) -> None:
    rc = script._run(
        decisions_file=decisions_file,
        canary_run_dir=canary_run_dir,
        model_key="qwen3-coder-local",
        kb_repo="OmniNode-ai/knowledge-base",
        dry_run=True,
    )
    assert rc == 0


@pytest.mark.unit
def test_dry_run_does_not_call_subprocess(
    canary_run_dir: Path, decisions_file: Path
) -> None:
    with patch("subprocess.run") as mock_run:
        script._run(
            decisions_file=decisions_file,
            canary_run_dir=canary_run_dir,
            model_key="qwen3-coder-local",
            kb_repo="OmniNode-ai/knowledge-base",
            dry_run=True,
        )
        mock_run.assert_not_called()


@pytest.mark.unit
def test_dry_run_does_not_clone_or_create_pr(
    canary_run_dir: Path, decisions_file: Path, tmp_path: Path
) -> None:
    with patch("subprocess.run") as mock_run:
        script._run(
            decisions_file=decisions_file,
            canary_run_dir=canary_run_dir,
            model_key="qwen3-coder-local",
            kb_repo="OmniNode-ai/knowledge-base",
            dry_run=True,
        )
    gh_calls = [
        c for c in mock_run.call_args_list if "gh" in (c.args[0] if c.args else [])
    ]
    assert gh_calls == []


# ---------------------------------------------------------------------------
# _run — live path (subprocess mocked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_live_run_calls_gh_clone_and_pr(
    canary_run_dir: Path, decisions_file: Path, tmp_path: Path
) -> None:
    def fake_subprocess(cmd: list[str], **kwargs: Any) -> MagicMock:
        if "clone" in cmd:
            # The clone target is the last arg; create it to satisfy subsequent git calls
            clone_dir = Path(cmd[-1])
            clone_dir.mkdir(parents=True, exist_ok=True)
            # Minimal git structure to satisfy subsequent git commands
            (clone_dir / ".git").mkdir(exist_ok=True)
        m = MagicMock()
        m.stdout = "https://github.com/OmniNode-ai/knowledge-base/pull/42\n"
        m.returncode = 0
        return m

    with (
        patch("subprocess.run", side_effect=fake_subprocess),
        patch(
            "omnimarket.adapters.adr.kb_adr_renderer.render_adr_to_kb"
        ) as mock_render,
    ):
        mock_render.return_value = MagicMock(
            adr_path=tmp_path / "fake.md",
            evidence_path=tmp_path / "fake-evidence.json",
        )
        rc = script._run(
            decisions_file=decisions_file,
            canary_run_dir=canary_run_dir,
            model_key="qwen3-coder-local",
            kb_repo="OmniNode-ai/knowledge-base",
            dry_run=False,
        )
    assert rc == 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_args_required_fields(tmp_path: Path) -> None:
    run_dir = tmp_path / "canary-2026-05-23-001"
    # Direct argparse test via sys.argv patching
    with patch(
        "sys.argv",
        [
            "publish_canary_adrs_to_kb.py",
            "--canary-run-dir",
            str(run_dir),
            "--model-key",
            "qwen3-coder-local",
        ],
    ):
        parsed = script._parse_args()
    assert parsed.canary_run_dir == run_dir
    assert parsed.model_key == "qwen3-coder-local"
    assert parsed.dry_run is False
    assert parsed.kb_repo == "OmniNode-ai/knowledge-base"


@pytest.mark.unit
def test_parse_args_dry_run_flag(tmp_path: Path) -> None:
    run_dir = tmp_path / "canary-run"
    with patch(
        "sys.argv",
        [
            "publish_canary_adrs_to_kb.py",
            "--canary-run-dir",
            str(run_dir),
            "--model-key",
            "deepseek-r1-local",
            "--dry-run",
        ],
    ):
        parsed = script._parse_args()
    assert parsed.dry_run is True


@pytest.mark.unit
def test_parse_args_custom_kb_repo(tmp_path: Path) -> None:
    run_dir = tmp_path / "canary-run"
    with patch(
        "sys.argv",
        [
            "publish_canary_adrs_to_kb.py",
            "--canary-run-dir",
            str(run_dir),
            "--model-key",
            "qwen3-coder-local",
            "--kb-repo",
            "my-org/my-kb",
        ],
    ):
        parsed = script._parse_args()
    assert parsed.kb_repo == "my-org/my-kb"
