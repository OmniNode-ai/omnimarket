"""Golden chain tests for node_doc_freshness_sweep.

Mocks the onex_change_control scanners to test the handler's aggregation
and report-generation logic without needing the full scanner dependency.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_doc_freshness_sweep.handlers.handler_doc_freshness_sweep import (
    DocFreshnessSweepRequest,
    NodeDocFreshnessSweep,
)

CMD_TOPIC = "onex.cmd.omnimarket.doc-freshness-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.doc-freshness-sweep-completed.v1"

_HMOD = "omnimarket.nodes.node_doc_freshness_sweep.handlers.handler_doc_freshness_sweep"

# Sentinel enum values — match what the handler uses for verdict comparisons
_VERDICT = SimpleNamespace(FRESH="FRESH", STALE="STALE", BROKEN="BROKEN")


def _make_fresh_result(doc_path: str, repo: str) -> MagicMock:
    r = MagicMock()
    r.doc_path = doc_path
    r.repo = repo
    r.verdict = _VERDICT.FRESH
    r.staleness_score = 0.0
    r.references = []
    r.broken_references = []
    r.stale_references = []
    return r


def _make_broken_result(doc_path: str, repo: str) -> MagicMock:
    ref = MagicMock()
    r = MagicMock()
    r.doc_path = doc_path
    r.repo = repo
    r.verdict = _VERDICT.BROKEN
    r.staleness_score = 0.8
    r.references = [ref]
    r.broken_references = [ref]
    r.stale_references = []
    return r


def _make_stale_result(doc_path: str, repo: str, score: float = 0.5) -> MagicMock:
    ref = MagicMock()
    r = MagicMock()
    r.doc_path = doc_path
    r.repo = repo
    r.verdict = _VERDICT.STALE
    r.staleness_score = score
    r.references = []
    r.broken_references = []
    r.stale_references = [ref]
    return r


def _patch_occ(build_return=None):
    """Context manager that patches all onex_change_control module-level symbols."""
    # Patch EnumDocStalenessVerdict so the handler's verdict == comparisons use our sentinels
    mock_report_cls = MagicMock()
    mock_report_cls.return_value = MagicMock()
    mock_repo_summary_cls = MagicMock()
    mock_repo_summary_cls.return_value = MagicMock()

    patches = [
        patch(f"{_HMOD}._OCC_AVAILABLE", True),
        patch(f"{_HMOD}.EnumDocStalenessVerdict", _VERDICT),
        patch(f"{_HMOD}.ModelDocFreshnessSweepReport", mock_report_cls),
        patch(f"{_HMOD}.ModelRepoDocSummary", mock_repo_summary_cls),
        patch(f"{_HMOD}.extract_all_references", return_value=[]),
        patch(f"{_HMOD}.resolve_references", return_value=[]),
        patch(f"{_HMOD}.get_recently_changed_files", return_value=set()),
    ]
    if build_return is not None:
        patches.append(
            patch(f"{_HMOD}.build_freshness_result", return_value=build_return)
        )
    return patches


@pytest.mark.unit
class TestDocFreshnessSweepGoldenChain:
    """Golden chain: command -> handler -> completion event."""

    async def test_healthy_when_no_issues(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """Handler should return healthy when no broken or stale docs found."""
        repo_dir = tmp_path / "test_repo"
        repo_dir.mkdir()
        (repo_dir / "README.md").write_text("# Hello\n")

        fresh = _make_fresh_result(str(repo_dir / "README.md"), "test_repo")

        patches = _patch_occ(build_return=fresh)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            handler = NodeDocFreshnessSweep()
            result = handler.handle(
                DocFreshnessSweepRequest(
                    omni_home=str(tmp_path), repos=["test_repo"], dry_run=True
                )
            )

        assert result.status == "healthy"
        assert result.total_docs == 1
        assert result.fresh_count == 1
        assert result.broken_count == 0

    async def test_issues_found_when_broken_docs(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """Handler should return issues_found when broken docs are present."""
        repo_dir = tmp_path / "test_repo"
        repo_dir.mkdir()
        (repo_dir / "README.md").write_text("See [missing](src/missing.py)\n")

        broken = _make_broken_result(str(repo_dir / "README.md"), "test_repo")

        patches = _patch_occ(build_return=broken)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            handler = NodeDocFreshnessSweep()
            result = handler.handle(
                DocFreshnessSweepRequest(
                    omni_home=str(tmp_path), repos=["test_repo"], dry_run=True
                )
            )

        assert result.status == "issues_found"
        assert result.broken_count == 1
        assert result.broken_reference_count == 1

    async def test_claude_md_only_filters_files(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """claude_md_only should only scan CLAUDE.md files."""
        repo_dir = tmp_path / "test_repo"
        repo_dir.mkdir()
        (repo_dir / "CLAUDE.md").write_text("# Claude\n")
        (repo_dir / "README.md").write_text("# Readme\n")

        scanned_files: list[str] = []

        mock_extract = MagicMock(
            side_effect=lambda doc_path: scanned_files.append(doc_path) or []
        )
        fresh = _make_fresh_result("placeholder", "test_repo")

        base_patches = _patch_occ(build_return=fresh)
        # Override extract_all_references with our capturing mock
        with (
            base_patches[0],
            base_patches[1],
            base_patches[2],
            base_patches[3],
            patch(f"{_HMOD}.extract_all_references", mock_extract),
            base_patches[5],
            base_patches[6],
            base_patches[7],
        ):
            handler = NodeDocFreshnessSweep()
            result = handler.handle(
                DocFreshnessSweepRequest(
                    omni_home=str(tmp_path),
                    repos=["test_repo"],
                    claude_md_only=True,
                    dry_run=True,
                )
            )

        assert all("CLAUDE.md" in f for f in scanned_files)
        assert result.total_docs == 1

    async def test_error_when_no_repos(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """Handler should return error when no valid repo directories found."""
        handler = NodeDocFreshnessSweep()
        result = handler.handle(
            DocFreshnessSweepRequest(
                omni_home=str(tmp_path), repos=["nonexistent_repo"], dry_run=True
            )
        )

        assert result.status == "error"
        assert result.error is not None

    async def test_top_stale_docs_sorted(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """Top stale docs should include the highest-scored stale docs."""
        repo_dir = tmp_path / "test_repo"
        repo_dir.mkdir()
        docs = ["doc_a.md", "doc_b.md", "doc_c.md"]
        for d in docs:
            (repo_dir / d).write_text(f"# {d}\n")

        scores = [0.3, 0.9, 0.6]
        idx = [0]

        def _build(doc_path: str, **kwargs: object) -> MagicMock:  # type: ignore[misc]
            r = _make_stale_result(doc_path, "test_repo", scores[idx[0] % len(scores)])
            idx[0] += 1
            return r

        base_patches = _patch_occ()
        with (
            base_patches[0],
            base_patches[1],
            base_patches[2],
            base_patches[3],
            base_patches[4],
            base_patches[5],
            base_patches[6],
            patch(f"{_HMOD}.build_freshness_result", side_effect=_build),
        ):
            handler = NodeDocFreshnessSweep()
            result = handler.handle(
                DocFreshnessSweepRequest(
                    omni_home=str(tmp_path), repos=["test_repo"], dry_run=True
                )
            )

        assert result.stale_count == 3
        assert len(result.top_stale_docs) == 3

    async def test_event_bus_wiring(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """Handler can publish completion event to EventBusInmemory."""
        repo_dir = tmp_path / "test_repo"
        repo_dir.mkdir()
        events_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            fresh = _make_fresh_result("placeholder", "test_repo")
            patches = _patch_occ(build_return=fresh)
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
                patches[5],
                patches[6],
                patches[7],
            ):
                handler = NodeDocFreshnessSweep()
                result = handler.handle(
                    DocFreshnessSweepRequest(
                        omni_home=payload.get("omni_home", str(tmp_path)),
                        repos=["test_repo"],
                        dry_run=True,
                    )
                )
            evt = {"status": result.status}
            events_captured.append(evt)
            await event_bus.publish(EVT_TOPIC, key=None, value=json.dumps(evt).encode())

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-doc-sweep"
        )

        cmd_payload = json.dumps({"omni_home": str(tmp_path)}).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(events_captured) == 1
        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()
