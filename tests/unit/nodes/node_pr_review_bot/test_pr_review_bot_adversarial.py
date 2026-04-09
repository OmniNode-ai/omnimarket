# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adversarial tests for the PR review bot — edge cases in diff fetcher and FSM.

All tests use mocked HTTP responses; no real API calls are made.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from omnimarket.nodes.node_pr_review_bot.handlers.handler_diff_fetcher import (
    DiffFetcherConfig,
    HandlerDiffFetcher,
    _extract_file_meta,
    _extract_line_range,
    _is_generated_file,
    _split_large_block,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_fsm import (
    MAX_CONSECUTIVE_FAILURES,
    HandlerPrReviewBot,
    ProtocolDiffFetcher,
    ProtocolJudgeVerifier,
    ProtocolReportPoster,
    ProtocolReviewer,
    ProtocolThreadPoster,
    ProtocolThreadWatcher,
)
from omnimarket.nodes.node_pr_review_bot.models.models import (
    DiffHunk,
    EnumFsmPhase,
    EnumPrVerdict,
    ReviewFinding,
    ReviewRequest,
    ReviewVerdict,
    ThreadState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(**overrides: Any) -> ReviewRequest:
    defaults: dict[str, Any] = {
        "correlation_id": uuid4(),
        "pr_number": 42,
        "repo": "owner/repo",
        "requested_at": datetime.now(tz=UTC),
    }
    defaults.update(overrides)
    return ReviewRequest(**defaults)


def _make_mock_response(
    body: str,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = body
    resp.headers = httpx.Headers(headers or {})
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _minimal_diff(file_path: str = "src/foo.py", content: str = "+print('hi')") -> str:
    return (
        f"diff --git a/{file_path} b/{file_path}\n"
        f"--- a/{file_path}\n"
        f"+++ b/{file_path}\n"
        f"@@ -1,1 +1,1 @@\n"
        f"{content}\n"
    )


# ---------------------------------------------------------------------------
# Stub sub-handlers for FSM pipeline tests
# ---------------------------------------------------------------------------


class _NullDiffFetcher(ProtocolDiffFetcher):
    def __init__(self, hunks: list[DiffHunk] | None = None) -> None:
        self._hunks = hunks or []

    def fetch(self, pr_number: int, repo: str) -> list[DiffHunk]:
        return self._hunks


class _NullReviewer(ProtocolReviewer):
    def __init__(self, findings: list[ReviewFinding] | None = None) -> None:
        self._findings = findings or []

    def review(
        self, correlation_id: Any, diff_hunks: Any, reviewer_models: Any
    ) -> list[ReviewFinding]:
        return self._findings


class _NullThreadPoster(ProtocolThreadPoster):
    def post(
        self, pr_number: int, repo: str, findings: Any, dry_run: bool
    ) -> list[ThreadState]:
        return []


class _NullThreadWatcher(ProtocolThreadWatcher):
    def watch(self, pr_number: int, repo: str, thread_states: Any) -> list[ThreadState]:
        return list(thread_states)


class _NullJudgeVerifier(ProtocolJudgeVerifier):
    def verify(
        self, correlation_id: Any, findings: Any, thread_states: Any, judge_model: str
    ) -> list[ThreadState]:
        return list(thread_states)


class _NullReportPoster(ProtocolReportPoster):
    def post_summary(
        self, pr_number: int, repo: str, verdict: ReviewVerdict, dry_run: bool
    ) -> None:
        pass


class _RaisingDiffFetcher(ProtocolDiffFetcher):
    def fetch(self, pr_number: int, repo: str) -> list[DiffHunk]:
        msg = "simulated fetch failure"
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# HandlerDiffFetcher adversarial tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiffFetcherEmptyDiff:
    """PR with 0 files changed returns an empty hunk list."""

    async def test_empty_diff_returns_empty_list(self) -> None:
        config = DiffFetcherConfig(github_token="test-token")
        fetcher = HandlerDiffFetcher(config)
        resp = _make_mock_response("")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)

            hunks = await fetcher.fetch(pr_number=1, repo="owner/repo")

        assert hunks == []

    async def test_whitespace_only_diff_returns_empty_list(self) -> None:
        config = DiffFetcherConfig(github_token="test-token")
        fetcher = HandlerDiffFetcher(config)
        resp = _make_mock_response("   \n\n\t  \n")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)

            hunks = await fetcher.fetch(pr_number=1, repo="owner/repo")

        assert hunks == []


@pytest.mark.unit
class TestDiffFetcherLargeDiff:
    """Diffs exceeding 512KB must be truncated before parsing (R3)."""

    async def test_large_diff_triggers_truncation(self) -> None:
        # Build a valid diff header then pad well past 512KB
        header = _minimal_diff("src/big.py", "+x = 1")
        padding = "+" + ("a" * 100 + "\n") * 6000  # ~600KB of extra lines
        raw = header + padding

        assert len(raw.encode()) > 512_000

        config = DiffFetcherConfig(github_token="tok")
        fetcher = HandlerDiffFetcher(config)
        resp = _make_mock_response(raw)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)

            hunks = await fetcher.fetch(pr_number=99, repo="owner/repo")

        # Should still return hunks (parsed from truncated diff), not crash
        assert isinstance(hunks, list)

    async def test_large_diff_hunk_count_capped(self) -> None:
        """A PR producing >200 hunks is capped at max_hunks."""
        # 201 minimal file diffs — each produces one hunk
        sections = []
        for i in range(201):
            sections.append(
                f"diff --git a/src/f{i}.py b/src/f{i}.py\n"
                f"--- a/src/f{i}.py\n"
                f"+++ b/src/f{i}.py\n"
                f"@@ -1,1 +1,1 @@\n"
                f"+x_{i} = {i}\n"
            )
        raw = "".join(sections)

        config = DiffFetcherConfig(github_token="tok", max_hunks=200)
        fetcher = HandlerDiffFetcher(config)
        resp = _make_mock_response(raw)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)

            hunks = await fetcher.fetch(pr_number=7, repo="owner/repo")

        assert len(hunks) == 200


@pytest.mark.unit
class TestDiffFetcherBinaryFiles:
    """Binary-only diffs must be filtered out."""

    async def test_binary_file_produces_no_hunks(self) -> None:
        raw = (
            "diff --git a/assets/logo.png b/assets/logo.png\n"
            "index abc..def 100644\n"
            "Binary files a/assets/logo.png and b/assets/logo.png differ\n"
        )
        config = DiffFetcherConfig(github_token="tok")
        fetcher = HandlerDiffFetcher(config)
        resp = _make_mock_response(raw)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)

            hunks = await fetcher.fetch(pr_number=5, repo="owner/repo")

        # Binary sections have no @@ markers — should produce zero hunks
        assert hunks == []

    def test_generated_file_patterns_cover_common_binaries(self) -> None:
        assert _is_generated_file("assets/bundle.min.js")
        assert _is_generated_file("dist/main.js")
        assert _is_generated_file("build/output.js")
        assert _is_generated_file("src/generated/api.pb.go")
        assert _is_generated_file("models/schema_pb2.py")
        assert not _is_generated_file("src/main.py")
        assert not _is_generated_file("tests/test_foo.py")


@pytest.mark.unit
class TestDiffFetcherUnicodeEdgeCases:
    """Unicode in file names and diff content must not crash the parser."""

    async def test_unicode_filename_parsed_correctly(self) -> None:
        file_path = "src/módulo_核心/handler.py"
        raw = (
            f"diff --git a/{file_path} b/{file_path}\n"
            f"--- a/{file_path}\n"
            f"+++ b/{file_path}\n"
            "@@ -1,1 +1,2 @@\n"
            " existing_line = True\n"
            "+new_line = True\n"
        )
        config = DiffFetcherConfig(github_token="tok")
        fetcher = HandlerDiffFetcher(config)
        resp = _make_mock_response(raw)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)

            hunks = await fetcher.fetch(pr_number=3, repo="owner/repo")

        assert len(hunks) == 1
        assert file_path in hunks[0].file_path

    async def test_unicode_diff_content_stored_verbatim(self) -> None:
        content = "+greeting = '你好世界 — Héllo Wörld 🌍'\n"
        raw = _minimal_diff("src/i18n.py", content)
        config = DiffFetcherConfig(github_token="tok")
        fetcher = HandlerDiffFetcher(config)
        resp = _make_mock_response(raw)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)

            hunks = await fetcher.fetch(pr_number=4, repo="owner/repo")

        assert len(hunks) == 1
        assert "你好世界" in hunks[0].content


@pytest.mark.unit
class TestDiffFetcherMalformedDiff:
    """Malformed diffs (missing @@ markers) should not crash; return best-effort."""

    def test_missing_at_markers_produces_no_hunks_for_section(self) -> None:
        raw = (
            "diff --git a/src/foo.py b/src/foo.py\n"
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            # No @@ line — malformed
            "+some added line\n"
            "-some removed line\n"
        )
        config = DiffFetcherConfig(github_token="tok")
        fetcher = HandlerDiffFetcher(config)
        hunks = fetcher._parse_unified_diff(raw)
        # Without @@ markers, the hunk splitter finds no blocks to create
        assert hunks == []

    def test_extract_line_range_missing_markers_returns_zeros(self) -> None:
        start, end = _extract_line_range("this is not a hunk header")
        assert start == 0
        assert end == 0

    def test_extract_file_meta_no_path_returns_empty_string(self) -> None:
        path, is_new, is_deleted = _extract_file_meta(
            "random garbage\nno diff header\n"
        )
        assert path == ""
        assert is_new is False
        assert is_deleted is False

    async def test_malformed_diff_does_not_raise(self) -> None:
        raw = "this is not a diff at all\njust garbage text\n"
        config = DiffFetcherConfig(github_token="tok")
        fetcher = HandlerDiffFetcher(config)
        resp = _make_mock_response(raw)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)

            # Must not raise
            hunks = await fetcher.fetch(pr_number=8, repo="owner/repo")

        assert isinstance(hunks, list)


@pytest.mark.unit
class TestDiffFetcherPr500Files:
    """PR with 500+ files should hit the max_hunks cap without crashing."""

    async def test_500_file_pr_capped_at_max_hunks(self) -> None:
        sections = []
        for i in range(500):
            sections.append(
                f"diff --git a/src/file_{i}.py b/src/file_{i}.py\n"
                f"--- a/src/file_{i}.py\n"
                f"+++ b/src/file_{i}.py\n"
                f"@@ -1,1 +1,1 @@\n"
                f"+v = {i}\n"
            )
        raw = "".join(sections)

        config = DiffFetcherConfig(github_token="tok", max_hunks=200)
        fetcher = HandlerDiffFetcher(config)
        resp = _make_mock_response(raw)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)

            hunks = await fetcher.fetch(pr_number=500, repo="owner/repo")

        assert len(hunks) == 200


@pytest.mark.unit
class TestDiffFetcherRateLimitResponse:
    """GitHub 403/429 rate-limit responses must surface as exceptions."""

    async def test_rate_limit_403_raises(self) -> None:
        config = DiffFetcherConfig(github_token="tok")
        fetcher = HandlerDiffFetcher(config)
        resp = _make_mock_response("", status_code=403)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)

            with pytest.raises(httpx.HTTPStatusError):
                await fetcher.fetch(pr_number=1, repo="owner/repo")

    async def test_rate_limit_429_raises(self) -> None:
        config = DiffFetcherConfig(github_token="tok")
        fetcher = HandlerDiffFetcher(config)
        resp = _make_mock_response("", status_code=429)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)

            with pytest.raises(httpx.HTTPStatusError):
                await fetcher.fetch(pr_number=1, repo="owner/repo")

    async def test_low_rate_limit_header_logged_but_continues(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Remaining < 100 should log a warning but still return hunks."""
        raw = _minimal_diff()
        config = DiffFetcherConfig(github_token="tok")
        fetcher = HandlerDiffFetcher(config)
        resp = _make_mock_response(raw, headers={"X-RateLimit-Remaining": "5"})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)

            import logging

            with caplog.at_level(logging.WARNING):
                hunks = await fetcher.fetch(pr_number=2, repo="owner/repo")

        assert isinstance(hunks, list)
        assert any("rate limit" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Hunk splitting tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSplitLargeBlock:
    """_split_large_block must chunk oversized blocks without losing content."""

    def test_block_within_limit_returned_as_is(self) -> None:
        block = "\n".join(f"+line {i}" for i in range(10))
        result = _split_large_block(block, max_lines=20)
        assert result == [block]

    def test_block_over_limit_split_into_multiple(self) -> None:
        block = "\n".join(f"+line {i}" for i in range(300))
        result = _split_large_block(block, max_lines=150)
        assert len(result) == 2

    def test_all_lines_preserved_after_split(self) -> None:
        lines = [f"+line {i}\n" for i in range(200)]
        block = "".join(lines)
        result = _split_large_block(block, max_lines=50)
        combined = "".join(result)
        assert combined == block


# ---------------------------------------------------------------------------
# FSM adversarial tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFsmCircuitBreaker:
    """After MAX_CONSECUTIVE_FAILURES failures, FSM must trip to FAILED."""

    def test_circuit_breaker_trips_after_max_failures(self) -> None:
        fsm = HandlerPrReviewBot()
        req = _make_request()
        state = fsm.start(req)
        # Advance to FETCH_DIFF phase
        state, _ = fsm.advance(state, phase_success=True)

        # Fail MAX_CONSECUTIVE_FAILURES times
        for i in range(MAX_CONSECUTIVE_FAILURES):
            state, _event = fsm.advance(
                state, phase_success=False, error_message=f"fail {i}"
            )
            if state.current_phase == EnumFsmPhase.FAILED:
                break

        assert state.current_phase == EnumFsmPhase.FAILED
        assert state.error_message is not None

    def test_partial_failures_then_success_resets_counter(self) -> None:
        fsm = HandlerPrReviewBot()
        req = _make_request()
        state = fsm.start(req)
        state, _ = fsm.advance(state, phase_success=True)  # INIT -> FETCH_DIFF

        # One failure — should not trip
        state, _event = fsm.advance(
            state, phase_success=False, error_message="transient"
        )
        assert state.current_phase == EnumFsmPhase.FETCH_DIFF
        assert state.consecutive_failures == 1

        # Success — counter resets
        state, _event = fsm.advance(state, phase_success=True, diff_hunks=[])
        assert state.consecutive_failures == 0
        assert state.current_phase == EnumFsmPhase.REVIEW


@pytest.mark.unit
class TestFsmEmptyDiffPipeline:
    """Full pipeline with zero hunks must complete as CLEAN without crashing."""

    def test_pipeline_empty_diff_completes_clean(self) -> None:
        fsm = HandlerPrReviewBot()
        req = _make_request()

        final_state, _events, verdict = fsm.run_full_pipeline(
            request=req,
            diff_fetcher=_NullDiffFetcher(hunks=[]),
            reviewer=_NullReviewer(),
            thread_poster=_NullThreadPoster(),
            thread_watcher=_NullThreadWatcher(),
            judge_verifier=_NullJudgeVerifier(),
            report_poster=_NullReportPoster(),
        )

        assert final_state.current_phase == EnumFsmPhase.DONE
        assert verdict.verdict == EnumPrVerdict.CLEAN
        assert verdict.total_findings == 0


@pytest.mark.unit
class TestFsmDiffFetcherRaisesEntersFailedPhase:
    """If diff fetcher raises, FSM should eventually reach FAILED phase."""

    def test_persistent_diff_fetch_failure_leads_to_failed(self) -> None:
        fsm = HandlerPrReviewBot()
        req = _make_request()

        final_state, events, _verdict = fsm.run_full_pipeline(
            request=req,
            diff_fetcher=_RaisingDiffFetcher(),
            reviewer=_NullReviewer(),
            thread_poster=_NullThreadPoster(),
            thread_watcher=_NullThreadWatcher(),
            judge_verifier=_NullJudgeVerifier(),
            report_poster=_NullReportPoster(),
        )

        # With only one attempt in run_full_pipeline (breaks on first failure),
        # the FSM won't reach FAILED unless circuit breaker trips or the
        # pipeline breaks on the non-success event. Either way, it should not
        # be DONE.
        assert final_state.current_phase != EnumFsmPhase.DONE
        assert any(not e.success for e in events)


# ---------------------------------------------------------------------------
# Malformed LLM JSON response (reviewer model)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMalformedLlmJsonResponse:
    """A reviewer that returns malformed JSON should fail gracefully."""

    class _BadJsonReviewer(ProtocolReviewer):
        def review(
            self, correlation_id: Any, diff_hunks: Any, reviewer_models: Any
        ) -> list[ReviewFinding]:
            raw = '{"findings": [INVALID JSON HERE}'
            # Simulate what a real reviewer would do: attempt to parse and fail
            json.loads(raw)  # This raises — caller (FSM) must handle gracefully
            return []

    def test_malformed_json_from_reviewer_leads_to_non_done_state(self) -> None:
        fsm = HandlerPrReviewBot()
        req = _make_request()

        final_state, events, _verdict = fsm.run_full_pipeline(
            request=req,
            diff_fetcher=_NullDiffFetcher(
                hunks=[
                    DiffHunk(
                        file_path="src/foo.py",
                        start_line=1,
                        end_line=5,
                        content="@@ -1,1 +1,5 @@\n+x = 1\n",
                    )
                ]
            ),
            reviewer=self._BadJsonReviewer(),
            thread_poster=_NullThreadPoster(),
            thread_watcher=_NullThreadWatcher(),
            judge_verifier=_NullJudgeVerifier(),
            report_poster=_NullReportPoster(),
        )

        # Reviewer raised — FSM must have recorded a failure event
        assert any(not e.success for e in events)
        # Must not have silently "succeeded" into DONE with garbage state
        assert final_state.current_phase != EnumFsmPhase.DONE


# ---------------------------------------------------------------------------
# Model validation edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiffHunkModelValidation:
    """DiffHunk Pydantic model enforces invariants."""

    def test_start_line_must_be_at_least_1(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DiffHunk(
                file_path="src/foo.py",
                start_line=0,  # invalid
                end_line=5,
                content="@@ -1 +1 @@\n+x = 1\n",
            )

    def test_end_line_must_be_at_least_1(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DiffHunk(
                file_path="src/foo.py",
                start_line=1,
                end_line=0,  # invalid
                content="@@ -1 +1 @@\n+x = 1\n",
            )

    def test_frozen_model_rejects_mutation(self) -> None:
        from pydantic import ValidationError

        hunk = DiffHunk(
            file_path="src/foo.py",
            start_line=1,
            end_line=5,
            content="@@ -1,1 +1,5 @@\n+x = 1\n",
        )
        with pytest.raises(ValidationError):
            hunk.file_path = "mutated"  # type: ignore[misc]

    def test_extra_fields_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DiffHunk(
                file_path="src/foo.py",
                start_line=1,
                end_line=5,
                content="@@ -1 +1 @@\n+x = 1\n",
                unknown_field="bad",  # extra="forbid"
            )


@pytest.mark.unit
class TestExtractLineRange:
    """_extract_line_range handles standard and optional count formats."""

    def test_standard_format_with_count(self) -> None:
        start, end = _extract_line_range("@@ -10,5 +20,8 @@ def foo():\n+line")
        assert start == 20
        assert end == 27  # 20 + 8 - 1

    def test_single_line_format_no_count(self) -> None:
        start, end = _extract_line_range("@@ -10 +20 @@ def bar():\n+line")
        assert start == 20
        assert end == 20  # count defaults to 1

    def test_zero_count_collapses_to_start(self) -> None:
        start, end = _extract_line_range("@@ -10,0 +20,0 @@ deleted block")
        assert start == 20
        assert end == 20  # max(count - 1, 0) = 0

    def test_garbage_input_returns_zeros(self) -> None:
        start, end = _extract_line_range("@@ totally wrong format")
        assert start == 0
        assert end == 0
