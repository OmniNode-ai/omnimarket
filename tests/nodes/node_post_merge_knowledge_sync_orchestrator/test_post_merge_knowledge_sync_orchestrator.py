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

Handler shape (OMN-14242): thin canonical --
``def handle(self, payload: ModelPostMergeSyncRequest) -> ModelSyncFanoutCommands``.
No envelope, no ``ModelHandlerOutput`` wrapper, no coercion in the handler --
the runtime wraps. The handler performs synchronous file I/O only (no
``await``), so it is sync -- same shape as the ``HandlerFrictionTriageOrchestrator``
precedent (same ORCHESTRATOR archetype, same "sync file I/O, no real await").
"""

from __future__ import annotations

import ast
import importlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_post_merge_knowledge_sync_orchestrator.handlers import (
    handler_post_merge_sync,
)
from omnimarket.nodes.node_post_merge_knowledge_sync_orchestrator.handlers.handler_post_merge_sync import (
    HandlerPostMergeSyncOrchestrator,
    evidence_base_dir,
)
from omnimarket.nodes.node_post_merge_knowledge_sync_orchestrator.models.model_sync_request import (
    ModelPostMergeSyncRequest,
    ModelSyncEvidence,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CORR_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
SYNC_RUN_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
MERGED_PR = 42
REPO = "OmniNode-ai/omnimarket"
COMMIT_SHA = "abc1234def5678"


@pytest.fixture(autouse=True)
def _sandbox_evidence_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the node's workspace root at a scratch directory.

    This sets the REAL variable rather than patching a module attribute
    (OMN-17459). Patching the constant left the resolution itself untested, and
    the resolution was the defect: it silently fell back to
    ``~/Code/omni_home``, a path that exists on the lab Macs and is TCC-denied
    to ``sshd``, so a governed remote run of this file raised PermissionError
    on ``mkdir`` in 11 tests that all passed locally.
    """
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    return


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


# ---------------------------------------------------------------------------
# Test: code-only change -> Repowise reindex only
# ---------------------------------------------------------------------------


class TestCodeOnlyChange:
    def test_code_only_triggers_repowise_only(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(changed_files=["src/omnimarket/nodes/foo/handler.py"])

        fanout = handler.handle(request)

        assert fanout.trigger_repowise_reindex is True
        assert fanout.trigger_memgraph_repopulation is False
        assert fanout.trigger_qdrant_antipattern_reindex is False
        assert fanout.trigger_kb_adr_canary is False

    def test_code_only_repowise_carries_correct_metadata(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(changed_files=["src/omnimarket/nodes/foo/handler.py"])

        fanout = handler.handle(request)

        assert fanout.affected_repo == REPO
        assert fanout.merged_pr == MERGED_PR
        assert fanout.source_commit_sha == COMMIT_SHA
        assert fanout.sync_run_id == SYNC_RUN_ID


# ---------------------------------------------------------------------------
# Test: contract.yaml change -> Memgraph repopulation
# ---------------------------------------------------------------------------


class TestContractChange:
    def test_contract_change_triggers_memgraph(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=[
                "src/omnimarket/nodes/node_foo/contract.yaml",
                "src/omnimarket/nodes/node_foo/handler.py",
            ]
        )

        fanout = handler.handle(request)

        assert fanout.trigger_repowise_reindex is True
        assert fanout.trigger_memgraph_repopulation is True
        assert fanout.trigger_qdrant_antipattern_reindex is False

    def test_multiple_contract_files_triggers_memgraph_once(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=[
                "src/omnimarket/nodes/node_foo/contract.yaml",
                "src/omnimarket/nodes/node_bar/contract.yaml",
            ]
        )

        fanout = handler.handle(request)

        assert fanout.trigger_memgraph_repopulation is True
        assert fanout.modified_contract_files == [
            "src/omnimarket/nodes/node_foo/contract.yaml",
            "src/omnimarket/nodes/node_bar/contract.yaml",
        ]


# ---------------------------------------------------------------------------
# Test: antipattern change -> Qdrant reindex
# ---------------------------------------------------------------------------


class TestAntipatternChange:
    def test_antipattern_file_triggers_qdrant(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/antipatterns/antipatterns.yaml"]
        )

        fanout = handler.handle(request)

        assert fanout.trigger_qdrant_antipattern_reindex is True
        assert fanout.trigger_repowise_reindex is True
        assert fanout.trigger_memgraph_repopulation is False

    def test_antipattern_json_triggers_qdrant(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(changed_files=["docs/antipatterns/catalog.json"])

        fanout = handler.handle(request)

        assert fanout.trigger_qdrant_antipattern_reindex is True

    def test_antipattern_and_contract_both_trigger(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=[
                "src/omnimarket/nodes/node_foo/contract.yaml",
                "src/omnimarket/antipatterns/antipatterns.yaml",
            ]
        )

        fanout = handler.handle(request)

        assert fanout.trigger_memgraph_repopulation is True
        assert fanout.trigger_qdrant_antipattern_reindex is True
        assert fanout.trigger_repowise_reindex is True


# ---------------------------------------------------------------------------
# Test: ADR-keyword content -> KB ADR canary
# ---------------------------------------------------------------------------


class TestADRKeywordTrigger:
    def test_adr_keyword_in_body_triggers_canary(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/nodes/foo/handler.py"],
            pr_body="This PR implements an ADR decision: use Kafka for all inter-service transport.",
        )

        fanout = handler.handle(request)

        assert fanout.trigger_kb_adr_canary is True

    def test_architecture_decision_keyword_triggers_canary(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/nodes/foo/handler.py"],
            pr_body="Architecture Decision: adopt deterministic replay as truth mechanism.",
        )

        fanout = handler.handle(request)

        assert fanout.trigger_kb_adr_canary is True

    def test_no_adr_keyword_does_not_trigger_canary(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/nodes/foo/handler.py"],
            pr_body="Fix a bug in the handler logic.",
        )

        fanout = handler.handle(request)

        assert fanout.trigger_kb_adr_canary is False

    def test_adr_keyword_in_title_triggers_canary(self) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/nodes/foo/handler.py"],
            pr_title="feat: ADR-0012 adopt contract-first design",
        )

        fanout = handler.handle(request)

        assert fanout.trigger_kb_adr_canary is True


# ---------------------------------------------------------------------------
# Test: evidence persistence
# ---------------------------------------------------------------------------


class TestEvidencePersistence:
    def test_evidence_written_to_disk(self, tmp_path: Path) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/nodes/node_foo/contract.yaml"],
        )

        # The autouse fixture already points OMNI_HOME at tmp_path, so this is
        # the directory the node itself resolves -- no module attribute is
        # patched, and the resolution is therefore under test (OMN-17459).
        evidence_dir = tmp_path / ".onex_state" / "knowledge-sync"
        handler.handle(request)

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

    def test_evidence_contains_fanout_flags(self, tmp_path: Path) -> None:
        handler = HandlerPostMergeSyncOrchestrator()
        request = _make_request(
            changed_files=["src/omnimarket/nodes/node_foo/contract.yaml"],
        )

        # The autouse fixture already points OMNI_HOME at tmp_path, so this is
        # the directory the node itself resolves -- no module attribute is
        # patched, and the resolution is therefore under test (OMN-17459).
        evidence_dir = tmp_path / ".onex_state" / "knowledge-sync"
        handler.handle(request)

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


def test_contract_declares_all_fanout_topics() -> None:
    """The orchestrator's contract declares every topic it can emit on: the
    terminal completed event plus the four conditional fan-out command topics."""
    import omnimarket.nodes.node_post_merge_knowledge_sync_orchestrator as node_pkg

    contract_text = (Path(node_pkg.__file__).parent / "contract.yaml").read_text()
    assert "onex.evt.omnimarket.post-merge-sync-completed.v1" in contract_text
    assert "onex.cmd.omnimarket.repowise-reindex-requested.v1" in contract_text
    assert "onex.cmd.omnimarket.memgraph-repopulation-requested.v1" in contract_text
    assert (
        "onex.cmd.omnimarket.qdrant-antipattern-reindex-requested.v1" in contract_text
    )
    assert "onex.cmd.omnimarket.adr-canary-requested.v1" in contract_text


# ---------------------------------------------------------------------------
# OMNI_HOME is required, and has no default (OMN-17459, omni_home rule 8)
# ---------------------------------------------------------------------------


class TestWorkspaceRootIsRequired:
    """The silent default was not a wrong value; it was a value that RESOLVED.

    ``os.environ.get("OMNI_HOME", str(Path.home() / "Code" / "omni_home"))``
    picks a directory that EXISTS on the lab Macs and is TCC-denied to
    ``sshd``. A governed pre-push dispatch of this repo's full suite to
    ``h101`` on 2026-09-01 therefore ran 17,883 tests in 13m58s and returned 12
    failures, 11 of them this node raising ``PermissionError: [Errno 1]
    Operation not permitted`` from ``mkdir`` -- every one of which passes
    locally. A missing required variable must say so, not resolve onto someone
    else's home directory.
    """

    def test_evidence_dir_refuses_to_guess_a_workspace_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OMNI_HOME", raising=False)
        with pytest.raises(KeyError) as excinfo:
            evidence_base_dir()
        message = str(excinfo.value)
        assert "OMNI_HOME" in message, "the refusal must name the missing variable"
        assert "no default" in message
        assert ".onex_state/knowledge-sync" in message, (
            "the refusal must name the layout it expected beneath the root"
        )

    def test_evidence_dir_never_resolves_under_the_home_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The specific path that produced the 11 remote failures must be
        unreachable from this resolver no matter what HOME says."""
        monkeypatch.delenv("OMNI_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        with pytest.raises(KeyError):
            evidence_base_dir()

        monkeypatch.setenv("OMNI_HOME", str(tmp_path / "registry"))
        assert evidence_base_dir() == (
            tmp_path / "registry" / ".onex_state" / "knowledge-sync"
        )

    def test_the_handler_module_carries_no_silent_env_default(self) -> None:
        """A structural pin: the shape rule 8 forbids must not come back into
        this module under another name.

        Driven off the AST, not the file text -- the docstring above
        deliberately QUOTES the forbidden call so the defect stays legible, and
        a substring scan cannot tell that quotation from a live call site.
        """
        tree = ast.parse(
            Path(handler_post_merge_sync.__file__).read_text(encoding="utf-8")
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call = ast.unparse(node.func)
            if call == "os.environ.get":
                assert not any(
                    isinstance(a, ast.Constant) and a.value == "OMNI_HOME"
                    for a in node.args
                ), "OMNI_HOME is read through .get() again, which cannot fail fast"
                assert len(node.args) < 2, (
                    f"a silent env default is back: {ast.unparse(node)}"
                )
            assert call != "Path.home", (
                f"a home-relative path default is back: {ast.unparse(node)}"
            )

    def test_the_resolution_is_lazy_so_import_never_needs_the_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CI runs this suite with no OMNI_HOME. A fail-fast MODULE CONSTANT
        would take the whole test module down at import instead of the one call
        that actually needs a workspace, so the fail-fast has to be lazy."""
        monkeypatch.delenv("OMNI_HOME", raising=False)
        importlib.reload(handler_post_merge_sync)
        assert handler_post_merge_sync.evidence_base_dir is not None
