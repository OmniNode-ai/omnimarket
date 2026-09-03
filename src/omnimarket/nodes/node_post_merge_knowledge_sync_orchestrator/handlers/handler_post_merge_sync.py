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

Thin canonical shape (OMN-14242): ``handle`` takes the typed request and
returns the typed ``ModelSyncFanoutCommands`` result directly — no envelope,
no ``ModelHandlerOutput`` wrapper, no coercion. The runtime wraps. The method
is sync: evidence persistence is plain synchronous file I/O with no
``await``-worthy operation, matching the ``HandlerFrictionTriageOrchestrator``
precedent (same ORCHESTRATOR archetype).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from omnimarket.nodes.node_post_merge_knowledge_sync_orchestrator.models.model_sync_request import (
    ModelPostMergeSyncRequest,
    ModelSyncEvidence,
    ModelSyncFanoutCommands,
)

_log = logging.getLogger(__name__)

_EVIDENCE_SUBPATH = (".onex_state", "knowledge-sync")


def evidence_base_dir() -> Path:
    """Where this node's durable evidence is written, resolved at CALL time.

    ``OMNI_HOME`` is REQUIRED and has no default (``omni_home/CLAUDE.md`` rule
    8). The previous form was ``os.environ.get("OMNI_HOME", str(Path.home() /
    "Code" / "omni_home"))``, which is the exact silent-default shape that rule
    forbids — and the default was itself the hardcoded-path shape rule 6
    forbids. It did not merely pick a wrong value; on the lab Macs it picked a
    directory that EXISTS and is TCC-denied to ``sshd``, so a governed pre-push
    run of this repo's suite on ``h101`` raised ``PermissionError: [Errno 1]
    Operation not permitted`` from ``mkdir`` against the pusher's own
    ``<home>/Code/omni_home/.onex_state/knowledge-sync``
    in 11 tests that all pass locally (OMN-17459, measured
    2026-09-01: 17,883 tests, 13m58s, 12 failures, one root cause). A
    fail-fast would have named the missing variable in one line instead.

    Resolution is LAZY on purpose. As a module-level constant this would raise
    at IMPORT, and CI runs this suite with no ``OMNI_HOME`` set — so a
    fail-fast constant would take the whole test module down rather than the
    one call that actually needs a workspace.
    """
    try:
        root = os.environ["OMNI_HOME"]
    except KeyError:
        raise KeyError(
            "OMNI_HOME is required to resolve the knowledge-sync evidence "
            "directory and has no default. Set it to the workspace registry "
            "root — the directory that holds the repo clones — so this node "
            f"can write {'/'.join(_EVIDENCE_SUBPATH)} beneath it."
        ) from None
    return Path(root).joinpath(*_EVIDENCE_SUBPATH)


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

    def handle(self, payload: ModelPostMergeSyncRequest) -> ModelSyncFanoutCommands:
        """Compute the conditional fan-out decision and persist evidence.

        Args:
            payload: The post-merge PR event to evaluate.

        Returns:
            ModelSyncFanoutCommands describing which knowledge backends to
            trigger for this merge.
        """
        request = payload
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

        return fanout

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

        evidence_dir = evidence_base_dir()
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_dir / f"{request.sync_run_id}.json"

        payload = evidence.model_dump(mode="json")
        # Serialize UUID fields as strings for human-readable JSON
        payload["sync_run_id"] = str(evidence.sync_run_id)

        evidence_path.write_text(json.dumps(payload, indent=2))
        _log.debug("Evidence written to %s", evidence_path)
