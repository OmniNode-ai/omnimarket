# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_post_merge_knowledge_sync_orchestrator [OMN-11930].

TDD: tests written before implementation.

Covers the conditional fan-out logic:
  - contract.yaml change -> Memgraph graph repopulation command emitted
  - antipattern entry change -> Qdrant reindex command emitted
  - code-only change -> Repowise reindex only
  - ADR-keyword content -> KB ADR publisher canary command emitted
  - evidence written to .onex_state/knowledge-sync/<sync_run_id>.json
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from omnimarket.nodes.node_post_merge_knowledge_sync_orchestrator.handlers.handler_post_merge_sync import (
    HandlerPostMergeSyncOrchestrator,
)
from omnimarket.nodes.node_post_merge_knowledge_sync_orchestrator.models.model_sync_request import (
    ModelPostMergeSyncRequest,
    ModelSyncEvidence,
    ModelSyncFanoutCommands,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CORR_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
SYNC_RUN_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
MERGED_PR = 42
REPO = "OmniNode-ai/omnimarket"
COMMIT_SHA = "abc1234def5678"


def _make_request(
    changed_files: list[str],
    pr_body: str = "",
    pr_title: str = "feat: some change",
    **overrides: Any,
) -> ModelPostMergeSyncRequest:
    defaults: dict[str, Any] = {
        "sync_run_id": SYNC_RUN_ID,
        "correlation_id": CORR_ID,
        "affected_repo": REPO,
        "merged_pr": MERGED_PR,
        "source_commit_sha": COMMIT_SHA,
        "changed_files": changed_files,
        "pr_body": pr_body,
        "pr_title": pr_title,
    }
    return ModelPostMergeSyncRequest(**{**defaults, **overrides})


def _get_fanout(output: Any) -> ModelSyncFanoutCommands:
    """Extract the ModelSyncFanoutCommands event from handler output events."""
    for evt in output.events:
        if isinstance(evt, ModelSyncFanoutCommands):
            return evt
    raise AssertionError(
        f"No ModelSyncFanoutCommands in output.events: {output.events}"
    )


# ---------------------------------------------------------------------------
# Test: code-only change -> Repowise reindex only
# ---------------------------------------------------------------------------


class TestCodeOnlyChange:
    @pytest.mark.asyncio
    async def test_code_only_triggers_repowise_only(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(changed_files=["src/omnimarket/nodes/foo/handler.py"])

        output = await handler.handle(request)
        fanout = _get_fanout(output)

        assert fanout.trigger_repowise_reindex is True
        assert fanout.trigger_memgraph_repopulation is False
        assert fanout.trigger_qdrant_antipattern_reindex is False
        assert fanout.trigger_kb_adr_canary is False

    @pytest.mark.asyncio
    async def test_code_only_repowise_carries_correct_metadata(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(changed_files=["src/omnimarket/nodes/foo/handler.py"])

        output = await handler.handle(request)
        fanout = _get_fanout(output)

        assert fanout.affected_repo == REPO
        assert fanout.merged_pr == MERGED_PR
        assert fanout.source_commit_sha == COMMIT_SHA
        assert fanout.sync_run_id == SYNC_RUN_ID


# ---------------------------------------------------------------------------
# Test: contract.yaml change -> Memgraph repopulation
# ---------------------------------------------------------------------------


class TestContractChange:
    @pytest.mark.asyncio
    async def test_contract_change_triggers_memgraph(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=[
                "src/omnimarket/nodes/node_foo/contract.yaml",
                "src/omnimarket/nodes/node_foo/handler.py",
            ]
        )

        output = await handler.handle(request)
        fanout = _get_fanout(output)

        assert fanout.trigger_repowise_reindex is True
        assert fanout.trigger_memgraph_repopulation is True
        assert fanout.trigger_qdrant_antipattern_reindex is False

    @pytest.mark.asyncio
    async def test_multiple_contract_files_triggers_memgraph_once(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=[
                "src/omnimarket/nodes/node_foo/contract.yaml",
                "src/omnimarket/nodes/node_bar/contract.yaml",
            ]
        )

        output = await handler.handle(request)
        fanout = _get_fanout(output)

        assert fanout.trigger_memgraph_repopulation is True
        assert fanout.modified_contract_files == [
            "src/omnimarket/nodes/node_foo/contract.yaml",
            "src/omnimarket/nodes/node_bar/contract.yaml",
        ]


# ---------------------------------------------------------------------------
# Test: antipattern change -> Qdrant reindex
# ---------------------------------------------------------------------------


class TestAntipatternChange:
    @pytest.mark.asyncio
    async def test_antipattern_file_triggers_qdrant(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/antipatterns/antipatterns.yaml"]
        )

        output = await handler.handle(request)
        fanout = _get_fanout(output)

        assert fanout.trigger_qdrant_antipattern_reindex is True
        assert fanout.trigger_repowise_reindex is True
        assert fanout.trigger_memgraph_repopulation is False

    @pytest.mark.asyncio
    async def test_antipattern_json_triggers_qdrant(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(changed_files=["docs/antipatterns/catalog.json"])

        output = await handler.handle(request)
        fanout = _get_fanout(output)

        assert fanout.trigger_qdrant_antipattern_reindex is True

    @pytest.mark.asyncio
    async def test_antipattern_and_contract_both_trigger(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=[
                "src/omnimarket/nodes/node_foo/contract.yaml",
                "src/omnimarket/antipatterns/antipatterns.yaml",
            ]
        )

        output = await handler.handle(request)
        fanout = _get_fanout(output)

        assert fanout.trigger_memgraph_repopulation is True
        assert fanout.trigger_qdrant_antipattern_reindex is True
        assert fanout.trigger_repowise_reindex is True


# ---------------------------------------------------------------------------
# Test: ADR-keyword content -> KB ADR canary
# ---------------------------------------------------------------------------


class TestADRKeywordTrigger:
    @pytest.mark.asyncio
    async def test_adr_keyword_in_body_triggers_canary(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/nodes/foo/handler.py"],
            pr_body="This PR implements an ADR decision: use Kafka for all inter-service transport.",
        )

        output = await handler.handle(request)
        fanout = _get_fanout(output)

        assert fanout.trigger_kb_adr_canary is True

    @pytest.mark.asyncio
    async def test_architecture_decision_keyword_triggers_canary(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/nodes/foo/handler.py"],
            pr_body="Architecture Decision: adopt deterministic replay as truth mechanism.",
        )

        output = await handler.handle(request)
        fanout = _get_fanout(output)

        assert fanout.trigger_kb_adr_canary is True

    @pytest.mark.asyncio
    async def test_no_adr_keyword_does_not_trigger_canary(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/nodes/foo/handler.py"],
            pr_body="Fix a bug in the handler logic.",
        )

        output = await handler.handle(request)
        fanout = _get_fanout(output)

        assert fanout.trigger_kb_adr_canary is False

    @pytest.mark.asyncio
    async def test_adr_keyword_in_title_triggers_canary(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/nodes/foo/handler.py"],
            pr_title="feat: ADR-0012 adopt contract-first design",
        )

        output = await handler.handle(request)
        fanout = _get_fanout(output)

        assert fanout.trigger_kb_adr_canary is True


# ---------------------------------------------------------------------------
# Test: evidence persistence
# ---------------------------------------------------------------------------


class TestEvidencePersistence:
    @pytest.mark.asyncio
    async def test_evidence_written_to_disk(self, tmp_path: Path) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/nodes/node_foo/contract.yaml"],
        )

        evidence_dir = tmp_path / ".onex_state" / "knowledge-sync"
        with patch(
            "omnimarket.nodes.node_post_merge_knowledge_sync_orchestrator.handlers.handler_post_merge_sync.EVIDENCE_BASE_DIR",
            evidence_dir,
        ):
            await handler.handle(request)

        evidence_file = evidence_dir / f"{SYNC_RUN_ID}.json"
        assert evidence_file.exists(), f"Expected evidence at {evidence_file}"

        data: dict[str, Any] = json.loads(evidence_file.read_text())
        assert data["sync_run_id"] == str(SYNC_RUN_ID)
        assert data["affected_repo"] == REPO
        assert data["merged_pr"] == MERGED_PR
        assert data["source_commit_sha"] == COMMIT_SHA
        assert "repowise_index_hash" in data
        assert "memgraph_snapshot_hash" in data
        assert "qdrant_collection_version" in data

    @pytest.mark.asyncio
    async def test_evidence_contains_fanout_flags(self, tmp_path: Path) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/nodes/node_foo/contract.yaml"],
        )

        evidence_dir = tmp_path / ".onex_state" / "knowledge-sync"
        with patch(
            "omnimarket.nodes.node_post_merge_knowledge_sync_orchestrator.handlers.handler_post_merge_sync.EVIDENCE_BASE_DIR",
            evidence_dir,
        ):
            await handler.handle(request)

        evidence_file = evidence_dir / f"{SYNC_RUN_ID}.json"
        data: dict[str, Any] = json.loads(evidence_file.read_text())

        assert data["trigger_repowise_reindex"] is True
        assert data["trigger_memgraph_repopulation"] is True
        assert data["trigger_qdrant_antipattern_reindex"] is False


# ---------------------------------------------------------------------------
# Test: ModelSyncEvidence fields
# ---------------------------------------------------------------------------


class TestModelSyncEvidence:
    def test_evidence_requires_sync_run_id(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ModelSyncEvidence(  # type: ignore[call-arg]
                affected_repo=REPO,
                merged_pr=MERGED_PR,
                source_commit_sha=COMMIT_SHA,
            )

    def test_evidence_instantiation(self) -> None:
        evidence = ModelSyncEvidence(
            sync_run_id=SYNC_RUN_ID,
            affected_repo=REPO,
            merged_pr=MERGED_PR,
            source_commit_sha=COMMIT_SHA,
            repowise_index_hash="hash-abc",
            memgraph_snapshot_hash="hash-def",
            qdrant_collection_version="v3",
            trigger_repowise_reindex=True,
            trigger_memgraph_repopulation=True,
            trigger_qdrant_antipattern_reindex=False,
            trigger_kb_adr_canary=False,
        )
        assert evidence.sync_run_id == SYNC_RUN_ID
        assert evidence.repowise_index_hash == "hash-abc"
