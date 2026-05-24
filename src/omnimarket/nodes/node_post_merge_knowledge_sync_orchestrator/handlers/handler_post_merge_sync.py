# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_post_merge_knowledge_sync_orchestrator [OMN-11930].

ORCHESTRATOR node. Consumes a post-merge PR event and conditionally fans out
to knowledge backends:

  (a) Repowise reindex     — always triggered on every merge
  (b) Memgraph repopulation — triggered when any contract.yaml was modified
  (c) Qdrant antipattern   — triggered when any antipattern catalog file was modified
  (d) KB ADR canary        — triggered when PR title/body contains ADR keywords

Produces durable evidence at .onex_state/knowledge-sync/<sync_run_id>.json.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_post_merge_knowledge_sync_orchestrator.models.model_sync_request import (
    ModelPostMergeSyncRequest,
    ModelSyncEvidence,
    ModelSyncFanoutCommands,
)

_log = logging.getLogger(__name__)

# Evidence output directory — patchable in tests
EVIDENCE_BASE_DIR: Path = (
    Path(os.environ.get("OMNI_HOME", str(Path.home() / "Code" / "omni_home")))
    / ".onex_state"
    / "knowledge-sync"
)

# File name patterns that indicate contract graph changes
_CONTRACT_FILENAME = "contract.yaml"

# Path fragments that indicate antipattern catalog files
_ANTIPATTERN_PATH_FRAGMENTS = ("antipattern",)
_ANTIPATTERN_EXTENSIONS = (".yaml", ".yml", ".json")

# ADR keyword heuristics (case-insensitive)
_ADR_KEYWORDS = (
    "adr",
    "adr-",
    "architecture decision",
    "architectural decision",
    "decision record",
)


def _is_contract_file(path: str) -> bool:
    return path.endswith(_CONTRACT_FILENAME) or f"/{_CONTRACT_FILENAME}" in path


def _is_antipattern_file(path: str) -> bool:
    lower = path.lower()
    has_fragment = any(frag in lower for frag in _ANTIPATTERN_PATH_FRAGMENTS)
    has_ext = any(lower.endswith(ext) for ext in _ANTIPATTERN_EXTENSIONS)
    return has_fragment and has_ext


def _has_adr_keyword(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _ADR_KEYWORDS)


class HandlerPostMergeSyncOrchestrator:
    """ORCHESTRATOR — conditionally fans out to knowledge backends after a PR merge."""

    async def handle(self, request: ModelPostMergeSyncRequest) -> ModelHandlerOutput:  # type: ignore[type-arg]
        modified_contract_files = [
            f for f in request.changed_files if _is_contract_file(f)
        ]
        modified_antipattern_files = [
            f for f in request.changed_files if _is_antipattern_file(f)
        ]

        trigger_memgraph = len(modified_contract_files) > 0
        trigger_qdrant = len(modified_antipattern_files) > 0
        trigger_adr = _has_adr_keyword(request.pr_title) or _has_adr_keyword(
            request.pr_body
        )

        fanout = ModelSyncFanoutCommands(
            sync_run_id=request.sync_run_id,
            affected_repo=request.affected_repo,
            merged_pr=request.merged_pr,
            source_commit_sha=request.source_commit_sha,
            trigger_repowise_reindex=True,
            trigger_memgraph_repopulation=trigger_memgraph,
            trigger_qdrant_antipattern_reindex=trigger_qdrant,
            trigger_kb_adr_canary=trigger_adr,
            modified_contract_files=modified_contract_files,
            modified_antipattern_files=modified_antipattern_files,
        )

        self._write_evidence(request, fanout)

        _log.info(
            "post-merge-sync sync_run_id=%s repo=%s pr=%s repowise=True memgraph=%s qdrant=%s adr=%s",
            request.sync_run_id,
            request.affected_repo,
            request.merged_pr,
            trigger_memgraph,
            trigger_qdrant,
            trigger_adr,
        )

        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_post_merge_knowledge_sync_orchestrator",
            events=(fanout,),
        )

    def _write_evidence(
        self,
        request: ModelPostMergeSyncRequest,
        fanout: ModelSyncFanoutCommands,
    ) -> None:
        evidence = ModelSyncEvidence(
            sync_run_id=request.sync_run_id,
            affected_repo=request.affected_repo,
            merged_pr=request.merged_pr,
            source_commit_sha=request.source_commit_sha,
            trigger_repowise_reindex=True,
            trigger_memgraph_repopulation=fanout.trigger_memgraph_repopulation,
            trigger_qdrant_antipattern_reindex=fanout.trigger_qdrant_antipattern_reindex,
            trigger_kb_adr_canary=fanout.trigger_kb_adr_canary,
            repowise_index_hash="",
            memgraph_snapshot_hash="",
            qdrant_collection_version="",
        )

        evidence_dir = Path(str(EVIDENCE_BASE_DIR))
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_dir / f"{request.sync_run_id}.json"

        payload = evidence.model_dump(mode="json")
        # Serialize UUID fields as strings for human-readable JSON
        payload["sync_run_id"] = str(evidence.sync_run_id)

        evidence_path.write_text(json.dumps(payload, indent=2))
        _log.debug("Evidence written to %s", evidence_path)
