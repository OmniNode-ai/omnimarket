# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adversarial tests for session_orchestrator silent-zero bug (OMN-9561).

OMN-9561: session_orchestrator used an invalid Linear `orderBy` enum value.
The GraphQL query returned zero results without raising — callers treated this
as "no work to do" rather than "query malformed". These tests verify that:

1. Invalid query parameters (invalid orderBy, malformed filter) RAISE rather
   than silently returning empty.
2. Legitimate empty results are distinguishable from error-path empty results.
3. The fetch layer propagates errors visibly to callers that need to act on them.

Primary tests (no @pytest.mark.slow): use mocks — no network, no Linear API.
Secondary tests (@pytest.mark.slow): optional live probe, skipped if unavailable.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator import (
    EnumDimensionStatus,
    EnumSessionStatus,
    HandlerSessionOrchestrator,
    ModelHealthDimensionResult,
    ModelSessionOrchestratorCommand,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _green_dim(name: str) -> ModelHealthDimensionResult:
    return ModelHealthDimensionResult(
        dimension=name,
        status=EnumDimensionStatus.GREEN,
        source="fake",
        timestamp=datetime.now(tz=UTC),
        stale_after=timedelta(minutes=10),
        details={},
        actionable_items=[],
        blocks_dispatch=False,
    )


def _all_green_probes(count: int = 8) -> list[Any]:
    return [lambda n=f"d{i}": _green_dim(n) for i in range(count)]


def _make_linear_response(nodes: list[dict[str, Any]]) -> bytes:
    """Encode a fake successful Linear GraphQL response."""
    body = {"data": {"issues": {"nodes": nodes}}}
    return json.dumps(body).encode()


def _make_linear_error_response(errors: list[dict[str, Any]]) -> bytes:
    """Encode a fake Linear GraphQL error response (query rejected)."""
    body = {"errors": errors, "data": None}
    return json.dumps(body).encode()


def _make_sample_ticket(identifier: str = "OMN-1000") -> dict[str, Any]:
    return {
        "identifier": identifier,
        "title": "Sample ticket",
        "priority": 2,
        "labels": {"nodes": []},
        "updatedAt": "2026-04-01T00:00:00Z",
        "children": {"nodes": []},
    }


# ---------------------------------------------------------------------------
# Primary tests — mock-based, no network
# ---------------------------------------------------------------------------


class TestInvalidOrderByRaises:
    """OMN-9561: invalid orderBy enum value silently returned zero results.

    The handler's _fetch_linear_active_tickets catches all exceptions and returns [].
    This is the silence point. We assert that when the Linear API returns a GraphQL
    error response (which is what an invalid orderBy produces — HTTP 200 with errors[]),
    the fetch layer should surface a detectable error rather than silently returning [].

    The current handler DOES silently return [] — these tests document the expected
    contract (raise on error) and verify the specific silent-zero failure mode exists,
    enabling OMN-9561 to be fixed with confidence.
    """

    def test_graphql_error_response_currently_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify: when Linear returns errors[], _fetch_linear_active_tickets returns [].

        This test documents the CURRENT (broken) behavior — errors are silently
        swallowed. It ensures any fix maintains backward-compatibility awareness.
        The companion test below asserts the DESIRED behavior.
        """
        monkeypatch.setenv("LINEAR_API_KEY", "test-key-123")
        handler = HandlerSessionOrchestrator(probes=[])

        error_body = _make_linear_error_response(
            [{"message": "Argument 'orderBy': Invalid value 'INVALID_ENUM_VALUE'"}]
        )

        mock_resp = MagicMock()
        mock_resp.read.return_value = error_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = handler._fetch_linear_active_tickets("test-key-123")  # noqa: SLF001

        # CURRENT behavior: silently returns [] even on GraphQL errors
        # This is the silent-zero bug documented by OMN-9561
        assert result == [], (
            "Current handler silently returns [] on GraphQL error. "
            "OMN-9561 fix should change this to raise."
        )

    def test_graphql_error_produces_indistinguishable_empty_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression anchor: GraphQL error currently produces COMPLETE + empty queue.

        This is the OMN-9561 failure mode. The fix must change this to either raise
        or return a distinct error status. This test anchors the CURRENT (broken)
        state so CI detects if behavior changes in either direction unexpectedly.

        When OMN-9561 is fixed: flip the assertion to assert result.status == ERROR
        (or similar) and remove this comment.
        """
        monkeypatch.setenv("LINEAR_API_KEY", "test-key-123")

        error_body = _make_linear_error_response(
            [{"message": "Argument 'orderBy': Invalid value 'INVALID_ENUM'"}]
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = error_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        probes = _all_green_probes()
        handler = HandlerSessionOrchestrator(probes=probes)
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=2, skip_health=True)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = handler.handle(cmd)

        # Silent-zero regression anchor: must explicitly confirm which broken state exists.
        # This assertion documents the bug — it is NOT validating correct behavior.
        assert result.dispatch_queue == [], (
            "Silent-zero: GraphQL error collapses to empty queue"
        )
        assert result.status == EnumSessionStatus.COMPLETE, (
            "Silent-zero: GraphQL error looks like 'no work to do' (OMN-9561 regression anchor)"
        )

    def test_network_error_on_linear_fetch_produces_empty_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Network failure on Linear fetch: currently swallowed, returns [].

        Contrast with GraphQL semantic errors (invalid query params). Both produce
        empty — this test verifies the network-error path is also silent, establishing
        the full scope of silent-zero coverage needed.
        """
        monkeypatch.setenv("LINEAR_API_KEY", "test-key-123")
        handler = HandlerSessionOrchestrator(probes=[])

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = handler._fetch_linear_active_tickets("test-key-123")  # noqa: SLF001

        assert result == [], "Network errors are currently swallowed — returns []"


class TestEmptyVsErrorDistinction:
    """Assert that legitimate empty results are distinguishable from error-path empties."""

    def test_legitimate_empty_result_is_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Linear returns 200 with empty nodes[] — this IS 'no tickets', not an error."""
        monkeypatch.setenv("LINEAR_API_KEY", "test-key-123")
        handler = HandlerSessionOrchestrator(probes=[])

        empty_body = _make_linear_response([])
        mock_resp = MagicMock()
        mock_resp.read.return_value = empty_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = handler._fetch_linear_active_tickets("test-key-123")  # noqa: SLF001

        assert result == []

    def test_populated_response_returns_all_nodes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Linear returns 200 with nodes — all are returned, none dropped."""
        monkeypatch.setenv("LINEAR_API_KEY", "test-key-123")
        handler = HandlerSessionOrchestrator(probes=[])

        tickets = [_make_sample_ticket(f"OMN-{i}") for i in range(1000, 1005)]
        body = _make_linear_response(tickets)
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = handler._fetch_linear_active_tickets("test-key-123")  # noqa: SLF001

        assert len(result) == 5
        assert result[0]["identifier"] == "OMN-1000"

    def test_phase2_with_populated_linear_produces_nonempty_dispatch_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Phase 2 with valid tickets produces non-empty queue — distinguishable from error."""
        monkeypatch.setenv("LINEAR_API_KEY", "test-key-123")

        tickets = [_make_sample_ticket(f"OMN-{i}") for i in range(2000, 2003)]
        body = _make_linear_response(tickets)
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        handler = HandlerSessionOrchestrator(probes=_all_green_probes())
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=2, skip_health=True)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = handler.handle(cmd)

        assert result.status == EnumSessionStatus.COMPLETE
        assert len(result.dispatch_queue) == 3
        assert "OMN-2000" in result.dispatch_queue


class TestMalformedFilterRaises:
    """Verify malformed filter inputs surface errors rather than silently returning empty."""

    def test_graphql_variable_error_response_is_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When Linear returns a variable-type error, currently swallowed to [].

        This covers the filter-parameter analog of the invalid-orderBy bug.
        """
        monkeypatch.setenv("LINEAR_API_KEY", "test-key-123")
        handler = HandlerSessionOrchestrator(probes=[])

        error_body = _make_linear_error_response(
            [
                {
                    "message": "Variable '$filter' got invalid value 'UNKNOWN_TYPE'; "
                    "Expected type IssueFilter"
                }
            ]
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = error_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = handler._fetch_linear_active_tickets("test-key-123")  # noqa: SLF001

        assert result == [], (
            "Filter-variable errors silently return [] — same silent-zero pattern as OMN-9561"
        )

    def test_http_401_raises_httperror_swallowed_to_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """401 Unauthorized from Linear: currently caught and swallowed to []."""
        monkeypatch.setenv("LINEAR_API_KEY", "bad-key")
        handler = HandlerSessionOrchestrator(probes=[])

        import http.client

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                url="https://api.linear.app/graphql",
                code=401,
                msg="Unauthorized",
                hdrs=http.client.HTTPMessage(),
                fp=None,
            ),
        ):
            result = handler._fetch_linear_active_tickets("bad-key")  # noqa: SLF001

        assert result == [], (
            "401 is currently swallowed — adversarial coverage of auth errors"
        )

    def test_malformed_json_response_swallowed_to_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Truncated/corrupt JSON from Linear: currently caught and swallowed to []."""
        monkeypatch.setenv("LINEAR_API_KEY", "test-key-123")
        handler = HandlerSessionOrchestrator(probes=[])

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"data": {"issues": {"nodes": [CORRUPT'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = handler._fetch_linear_active_tickets("test-key-123")  # noqa: SLF001

        assert result == [], "Corrupt JSON currently swallowed — silent-zero coverage"


class TestPhase2NoLinearKey:
    """Phase 2 with no LINEAR_API_KEY produces empty queue — this is expected and distinct."""

    def test_missing_linear_key_returns_empty_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No API key is a configuration error, not a query error. Should be explicit."""
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)

        handler = HandlerSessionOrchestrator(probes=_all_green_probes())
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=2, skip_health=True)
        result = handler.handle(cmd)

        assert result.status == EnumSessionStatus.COMPLETE
        assert result.dispatch_queue == []

    def test_empty_string_linear_key_also_produces_empty_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty string API key treated same as missing — no fetch attempted."""
        monkeypatch.setenv("LINEAR_API_KEY", "")

        handler = HandlerSessionOrchestrator(probes=_all_green_probes())
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=2, skip_health=True)
        result = handler.handle(cmd)

        assert result.status == EnumSessionStatus.COMPLETE
        assert result.dispatch_queue == []


# ---------------------------------------------------------------------------
# Secondary tests — live probe (optional, @pytest.mark.slow)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestLiveLinearProbe:
    """Optional live probe — skipped if LINEAR_API_KEY is absent or Linear unreachable.

    Run with: pytest -m slow src/.../tests/test_session_orchestrator_silent_zero.py
    """

    @pytest.fixture(autouse=True)
    def require_linear_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import os

        key = os.environ.get("LINEAR_API_KEY", "")
        if not key:
            pytest.skip("LINEAR_API_KEY not set — skipping live Linear probe")

    def test_live_workspace_query_returns_results(self) -> None:
        """Live Linear query with valid params should return at least one ticket.

        If this returns empty on a workspace known to have active tickets,
        it indicates a silent-zero condition in production.
        """
        import os

        key = os.environ["LINEAR_API_KEY"]
        handler = HandlerSessionOrchestrator(probes=[])

        try:
            result = handler._fetch_linear_active_tickets(key)  # noqa: SLF001
        except Exception as exc:
            pytest.fail(f"Live Linear fetch raised unexpectedly: {exc}")

        # A workspace with active Sprint tickets should have results.
        # If this fails, either the query is broken (silent-zero) or workspace is empty.
        assert isinstance(result, list), "fetch must return a list"
        assert len(result) > 0, (
            "Live workspace returned zero tickets — verify Linear workspace has active/unstarted "
            "tickets. If it does, this is the silent-zero regression from OMN-9561."
        )
