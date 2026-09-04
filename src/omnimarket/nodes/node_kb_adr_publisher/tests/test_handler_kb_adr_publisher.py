# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerKBADRPublisher (OMN-11808)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from omnimarket.models.adr import (
    EnumAdrKBDestination,
    ModelAdrSourceProvenance,
)
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

_SOURCE_PROVENANCE = ModelAdrSourceProvenance(
    source_repository="OmniNode-ai/omnimarket",
    source_visibility="public",
    publication_classification="public",
)


def _make_decision(model_id: str = "qwen3-coder-local") -> dict[str, Any]:
    return {
        "draft": {
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
        },
        "source_provenance": _SOURCE_PROVENANCE.model_dump(mode="json"),
        "kb_destination": "public",
        "source_documents": [
            {
                "source_path": "docs/plans/adr-publisher-plan.md",
                "source_content_sha256": "a" * 64,
            }
        ],
    }


def _publish_request(
    canary_run_dir: Path,
    *,
    model_key: str = "qwen3-coder-local",
    dry_run: bool = False,
    kb_destination: EnumAdrKBDestination | None = EnumAdrKBDestination.public,
    source_provenance: ModelAdrSourceProvenance | None = _SOURCE_PROVENANCE,
) -> ModelKBADRPublishRequest:
    return ModelKBADRPublishRequest(
        canary_run_dir=str(canary_run_dir),
        model_key=model_key,
        dry_run=dry_run,
        kb_destination=kb_destination,
        source_provenance=source_provenance,
    )


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
        assert r.draft.extraction_metadata.model_id == "qwen3-coder-local"


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
    request = _publish_request(run_dir)
    result = await HandlerKBADRPublisher().handle(request=request)
    assert isinstance(result, ModelKBADRPublishResult)
    assert result.success is False
    assert result.error is not None
    assert "extracted_decisions.json" in result.error


@pytest.mark.unit
async def test_handle_returns_failure_when_no_matching_decisions(
    canary_run_dir: Path, decisions_file: Path
) -> None:
    request = _publish_request(canary_run_dir, model_key="nonexistent-model")
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
    request = _publish_request(canary_run_dir, dry_run=True)
    result = await HandlerKBADRPublisher().handle(request=request)
    assert result.success is True
    assert result.adr_count == 2
    assert result.pr_url is None
    assert result.branch is None


@pytest.mark.unit
async def test_dry_run_does_not_call_subprocess(
    canary_run_dir: Path, decisions_file: Path
) -> None:
    request = _publish_request(canary_run_dir, dry_run=True)
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
        request = _publish_request(canary_run_dir)
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
        request = _publish_request(canary_run_dir)
        await HandlerKBADRPublisher().handle(request=request)

    clone_calls = [c for c in calls if "clone" in c]
    pr_calls = [c for c in calls if "pr" in c and "create" in c]
    assert len(clone_calls) == 1
    assert len(pr_calls) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source_provenance", "kb_destination", "error_fragment"),
    [
        (None, EnumAdrKBDestination.public, "source_provenance is required"),
        (
            ModelAdrSourceProvenance(
                source_repository="OmniNode-ai/private-source",
                source_visibility="private",
                publication_classification="private",
            ),
            EnumAdrKBDestination.public,
            "conflicts",
        ),
        (
            ModelAdrSourceProvenance(
                source_repository="OmniNode-ai/omnimarket",
                source_visibility="public",
                publication_classification="restricted",
            ),
            EnumAdrKBDestination.public,
            "conflicts",
        ),
    ],
)
async def test_mismatched_or_missing_provenance_rejects_before_subprocess(
    canary_run_dir: Path,
    decisions_file: Path,
    source_provenance: ModelAdrSourceProvenance | None,
    kb_destination: EnumAdrKBDestination,
    error_fragment: str,
) -> None:
    """The first policy failure occurs before clone/git/gh subprocess calls."""
    runner = MagicMock()
    result = await HandlerKBADRPublisher(run=runner).handle(
        _publish_request(
            canary_run_dir,
            source_provenance=source_provenance,
            kb_destination=kb_destination,
        )
    )
    assert result.success is False
    assert result.error_code == "PUBLICATION_POLICY_REJECTED"
    assert result.error is not None
    assert error_fragment in result.error
    runner.assert_not_called()


@pytest.mark.unit
async def test_private_source_cannot_publish_to_public_before_subprocess(
    canary_run_dir: Path,
    decisions_file: Path,
) -> None:
    private_source = ModelAdrSourceProvenance(
        source_repository="OmniNode-ai/private-source",
        source_visibility="private",
        publication_classification="private",
    )
    decisions = json.loads(decisions_file.read_text(encoding="utf-8"))
    for decision in decisions:
        decision["source_provenance"] = private_source.model_dump(mode="json")
    decisions_file.write_text(json.dumps(decisions), encoding="utf-8")
    runner = MagicMock()
    result = await HandlerKBADRPublisher(run=runner).handle(
        _publish_request(
            canary_run_dir,
            source_provenance=private_source,
            kb_destination=EnumAdrKBDestination.public,
        )
    )
    assert result.success is False
    assert result.error_code == "PUBLICATION_POLICY_REJECTED"
    assert result.error is not None
    assert "private source provenance" in result.error
    runner.assert_not_called()


@pytest.mark.unit
async def test_restricted_source_cannot_publish_to_public_before_subprocess(
    canary_run_dir: Path,
    decisions_file: Path,
) -> None:
    restricted_source = ModelAdrSourceProvenance(
        source_repository="OmniNode-ai/omnimarket",
        source_visibility="public",
        publication_classification="restricted",
    )
    decisions = json.loads(decisions_file.read_text(encoding="utf-8"))
    for decision in decisions:
        decision["source_provenance"] = restricted_source.model_dump(mode="json")
    decisions_file.write_text(json.dumps(decisions), encoding="utf-8")
    runner = MagicMock()
    result = await HandlerKBADRPublisher(run=runner).handle(
        _publish_request(
            canary_run_dir,
            source_provenance=restricted_source,
            kb_destination=EnumAdrKBDestination.public,
        )
    )
    assert result.success is False
    assert result.error_code == "PUBLICATION_POLICY_REJECTED"
    assert result.error is not None
    assert "restricted publication classification" in result.error
    runner.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutation", "error_code", "error_fragment"),
    [
        (
            "missing_provenance",
            "PUBLICATION_POLICY_REJECTED",
            "missing source_provenance",
        ),
        (
            "missing_source_documents",
            "PUBLICATION_POLICY_REJECTED",
            "missing hash-pinned source_documents",
        ),
        (
            "unknown_classification",
            "INVALID_CANDIDATE_EVIDENCE",
            "publication_classification",
        ),
        (
            "conflicting_classification",
            "INVALID_CANDIDATE_EVIDENCE",
            "private source visibility conflicts",
        ),
    ],
)
async def test_malformed_or_incomplete_candidate_evidence_never_reaches_subprocess(
    canary_run_dir: Path,
    decisions_file: Path,
    mutation: str,
    error_code: str,
    error_fragment: str,
) -> None:
    """Unknown, conflicting, or incomplete source metadata fails closed."""
    decisions = json.loads(decisions_file.read_text(encoding="utf-8"))
    for decision in decisions:
        if mutation == "missing_provenance":
            decision.pop("source_provenance")
        elif mutation == "missing_source_documents":
            decision["source_documents"] = []
        elif mutation == "unknown_classification":
            decision["source_provenance"]["publication_classification"] = "unknown"
        else:
            decision["source_provenance"] = {
                "source_repository": "OmniNode-ai/conflicting-source",
                "source_visibility": "private",
                "publication_classification": "public",
            }
    decisions_file.write_text(json.dumps(decisions), encoding="utf-8")

    runner = MagicMock()
    result = await HandlerKBADRPublisher(run=runner).handle(
        _publish_request(canary_run_dir)
    )
    assert result.success is False
    assert result.error_code == error_code
    assert result.error is not None
    assert error_fragment in result.error
    runner.assert_not_called()


@pytest.mark.unit
async def test_missing_destination_never_reaches_subprocess(
    canary_run_dir: Path, decisions_file: Path
) -> None:
    runner = MagicMock()
    result = await HandlerKBADRPublisher(run=runner).handle(
        _publish_request(canary_run_dir, kb_destination=None)
    )
    assert result.success is False
    assert result.error_code == "PUBLICATION_POLICY_REJECTED"
    assert result.error is not None
    assert "kb_destination is required" in result.error
    runner.assert_not_called()


@pytest.mark.unit
async def test_public_source_can_publish_to_private_contract_destination(
    canary_run_dir: Path,
    decisions_file: Path,
    tmp_path: Path,
) -> None:
    decisions = json.loads(decisions_file.read_text(encoding="utf-8"))
    for decision in decisions:
        decision["kb_destination"] = "private"
    decisions_file.write_text(json.dumps(decisions), encoding="utf-8")
    calls: list[list[str]] = []

    def fake_subprocess(cmd: list[str], **kwargs: Any) -> MagicMock:
        calls.append(cmd)
        if "clone" in cmd:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        response = MagicMock()
        response.stdout = (
            "https://github.com/OmniNode-ai/knowledge-base-internal/pull/7\n"
        )
        return response

    with (
        patch("subprocess.run", side_effect=fake_subprocess),
        patch(
            "omnimarket.nodes.node_kb_adr_publisher.handlers.handler_kb_adr_publisher.render_adr_to_kb"
        ) as mock_render,
    ):
        mock_render.return_value = MagicMock(adr_path=tmp_path / "fake.md")
        result = await HandlerKBADRPublisher().handle(
            _publish_request(
                canary_run_dir,
                kb_destination=EnumAdrKBDestination.private,
            )
        )
    assert result.success is True
    assert result.kb_repository == "OmniNode-ai/knowledge-base-internal"
    assert ["gh", "repo", "clone", "OmniNode-ai/knowledge-base-internal"] in [
        call[:4] for call in calls
    ]


@pytest.mark.unit
def test_request_rejects_arbitrary_kb_repository_authority(
    canary_run_dir: Path,
) -> None:
    with pytest.raises(ValidationError):
        ModelKBADRPublishRequest.model_validate(
            {
                "canary_run_dir": str(canary_run_dir),
                "model_key": "qwen3-coder-local",
                "kb_repo": "attacker/knowledge-base",
            }
        )


@pytest.mark.unit
def test_source_provenance_allows_narrower_private_classification() -> None:
    source = ModelAdrSourceProvenance(
        source_repository="OmniNode-ai/public-source",
        source_visibility="public",
        publication_classification="private",
    )
    assert source.publication_classification.value == "private"


@pytest.mark.unit
def test_source_provenance_rejects_private_source_labeled_public() -> None:
    with pytest.raises(ValidationError, match="private source visibility conflicts"):
        ModelAdrSourceProvenance(
            source_repository="OmniNode-ai/private-source",
            source_visibility="private",
            publication_classification="public",
        )


# ---------------------------------------------------------------------------
# Sanitization and subprocess boundary
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "unsafe_context",
    [
        'api_key: "very-secret-value"',
        "See OmniNode-ai/omni_home for the private source plan.",
        "The source was checked out at /private/workspace/docs/plan.md.",
    ],
)
async def test_public_sanitization_rejects_before_any_subprocess(
    canary_run_dir: Path,
    decisions_file: Path,
    unsafe_context: str,
) -> None:
    """Public output is rejected at the process boundary, not after clone."""
    decisions = json.loads(decisions_file.read_text(encoding="utf-8"))
    for decision in decisions:
        decision["draft"]["context"] = unsafe_context
    decisions_file.write_text(json.dumps(decisions), encoding="utf-8")

    runner = MagicMock()
    result = await HandlerKBADRPublisher(run=runner).handle(
        _publish_request(canary_run_dir)
    )

    assert result.success is False
    assert result.error_code == "SANITIZATION_REJECTED"
    runner.assert_not_called()


@pytest.mark.unit
async def test_private_destination_allows_private_references_but_not_secrets(
    canary_run_dir: Path,
    decisions_file: Path,
) -> None:
    """Internal publication may retain private refs, while secrets stay blocked."""
    private_source = ModelAdrSourceProvenance(
        source_repository="OmniNode-ai/omni_home",
        source_visibility="private",
        publication_classification="restricted",
    )
    decisions = json.loads(decisions_file.read_text(encoding="utf-8"))
    for decision in decisions:
        decision["source_provenance"] = private_source.model_dump(mode="json")
        decision["kb_destination"] = "private"
        decision["draft"]["context"] = (
            "Internal source OmniNode-ai/omni_home at /private/workspace/plan.md."
        )
    decisions_file.write_text(json.dumps(decisions), encoding="utf-8")

    runner = MagicMock()
    result = await HandlerKBADRPublisher(run=runner).handle(
        _publish_request(
            canary_run_dir,
            dry_run=True,
            kb_destination=EnumAdrKBDestination.private,
            source_provenance=private_source,
        )
    )

    assert result.success is True
    runner.assert_not_called()

    for decision in decisions:
        decision["draft"]["context"] = 'token: "very-secret-value"'
    decisions_file.write_text(json.dumps(decisions), encoding="utf-8")
    result = await HandlerKBADRPublisher(run=runner).handle(
        _publish_request(
            canary_run_dir,
            dry_run=True,
            kb_destination=EnumAdrKBDestination.private,
            source_provenance=private_source,
        )
    )
    assert result.success is False
    assert result.error_code == "SANITIZATION_REJECTED"
    runner.assert_not_called()


@pytest.mark.unit
async def test_public_validator_runs_before_git_mutation_with_contract_timeout(
    canary_run_dir: Path,
    decisions_file: Path,
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_subprocess(cmd: list[str], **kwargs: Any) -> MagicMock:
        calls.append((list(cmd), kwargs))
        if "clone" in cmd:
            clone_dir = Path(cmd[-1])
            clone_dir.mkdir(parents=True, exist_ok=True)
        response = MagicMock()
        response.stdout = "https://github.com/OmniNode-ai/knowledge-base/pull/42\n"
        response.returncode = 0
        return response

    with patch(
        "omnimarket.nodes.node_kb_adr_publisher.handlers.handler_kb_adr_publisher.render_adr_to_kb"
    ) as render:
        render.return_value = MagicMock(adr_path=tmp_path / "fake.md")
        result = await HandlerKBADRPublisher(run=fake_subprocess).handle(
            _publish_request(canary_run_dir)
        )

    assert result.success is True
    commands = [command for command, _ in calls]
    validator_index = commands.index(["uv", "run", "python", "scripts/validate.py"])
    add_index = next(
        index for index, command in enumerate(commands) if "add" in command
    )
    assert validator_index < add_index
    assert all(kwargs.get("timeout") == 120 for _, kwargs in calls)
