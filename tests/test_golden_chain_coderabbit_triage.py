"""Golden chain test for node_coderabbit_triage.

Verifies the keyword-based classification logic and event bus wiring.
All tests that test classification use the classify_body method directly
(no subprocess calls). Event bus tests use dry_run mode.

GraphQL shape tests verify that the handler correctly parses the
reviewThreads GraphQL response and produces non-zero total_threads for
PRs where CodeRabbit posts findings as review thread objects.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_coderabbit_triage.handlers.handler_coderabbit_triage import (
    EnumThreadSeverity,
    HandlerCoderabbitTriage,
    ModelCoderabbitTriageCommand,
    ModelThreadClassification,
)

CMD_TOPIC = "onex.cmd.omnimarket.coderabbit-triage-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.coderabbit-triage-completed.v1"


# ---------------------------------------------------------------------------
# Helpers — GraphQL response shape factories
# ---------------------------------------------------------------------------


def _make_graphql_thread(
    *,
    author_login: str = "coderabbitai[bot]",
    body: str = "nitpick: prefer f-strings",
    database_id: int = 101,
    url: str = "https://github.com/example/pr/files#r101",
    is_resolved: bool = False,
) -> dict[str, Any]:
    """Build a single reviewThreads node in the GraphQL response shape."""
    return {
        "id": f"PRT_{database_id}",
        "isResolved": is_resolved,
        "comments": {
            "nodes": [
                {
                    "databaseId": database_id,
                    "author": {"login": author_login},
                    "body": body,
                    "path": "src/foo.py",
                    "url": url,
                }
            ]
        },
    }


@pytest.mark.unit
class TestCoderabbitTriageGoldenChain:
    """Golden chain: classification logic + event bus wiring."""

    # --- Unit tests for classify_body ---

    async def test_critical_keyword_is_blocking(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Body containing 'critical' should be classified BLOCKING."""
        handler = HandlerCoderabbitTriage()
        severity, keyword = handler.classify_body("This is a critical security issue.")
        assert severity == EnumThreadSeverity.BLOCKING
        assert keyword == "critical"

    async def test_security_keyword_is_blocking(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Body containing 'security' should be classified BLOCKING."""
        handler = HandlerCoderabbitTriage()
        severity, keyword = handler.classify_body(
            "Security: this pattern leaks credentials."
        )
        assert severity == EnumThreadSeverity.BLOCKING
        assert keyword == "security"

    async def test_bug_keyword_is_blocking(self, event_bus: EventBusInmemory) -> None:
        """Body containing 'bug' should be classified BLOCKING."""
        handler = HandlerCoderabbitTriage()
        severity, _ = handler.classify_body("This looks like a bug in the logic.")
        assert severity == EnumThreadSeverity.BLOCKING

    async def test_nitpick_keyword_is_suggestion(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Body containing 'nitpick' should be classified SUGGESTION."""
        handler = HandlerCoderabbitTriage()
        severity, keyword = handler.classify_body("nitpick: prefer single quotes here.")
        assert severity == EnumThreadSeverity.SUGGESTION
        assert "nit" in keyword

    async def test_style_keyword_is_suggestion(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Body containing 'style' or 'prefer' should be classified SUGGESTION."""
        handler = HandlerCoderabbitTriage()
        severity, keyword = handler.classify_body(
            "Style preference: use snake_case consistently."
        )
        assert severity == EnumThreadSeverity.SUGGESTION
        # Either 'style' or 'prefer' may match first (sorted iteration order)
        assert keyword in ("style", "prefer")

    async def test_consider_keyword_is_suggestion(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Body containing 'consider' should be classified SUGGESTION."""
        handler = HandlerCoderabbitTriage()
        severity, _ = handler.classify_body(
            "Consider extracting this into a helper function."
        )
        assert severity == EnumThreadSeverity.SUGGESTION

    async def test_unknown_body_is_unknown(self, event_bus: EventBusInmemory) -> None:
        """Body with no matching keywords should be classified UNKNOWN."""
        handler = HandlerCoderabbitTriage()
        severity, keyword = handler.classify_body("The variable name looks fine to me.")
        assert severity == EnumThreadSeverity.UNKNOWN
        assert keyword == ""

    async def test_blocking_takes_priority_over_suggestion(
        self, event_bus: EventBusInmemory
    ) -> None:
        """When both BLOCKING and SUGGESTION keywords present, BLOCKING wins."""
        handler = HandlerCoderabbitTriage()
        severity, _ = handler.classify_body(
            "This is a critical issue, but just a style suggestion."
        )
        assert severity == EnumThreadSeverity.BLOCKING

    async def test_case_insensitive_classification(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Classification should be case-insensitive."""
        handler = HandlerCoderabbitTriage()
        severity_upper, _ = handler.classify_body("CRITICAL: fix this now")
        severity_lower, _ = handler.classify_body("critical: fix this now")
        assert severity_upper == EnumThreadSeverity.BLOCKING
        assert severity_lower == EnumThreadSeverity.BLOCKING

    # --- GraphQL shape tests (regression for REST /comments false-clean bug) ---

    async def test_graphql_thread_shape_produces_nonzero_total(
        self, event_bus: EventBusInmemory
    ) -> None:
        """PR with CodeRabbit review threads (GraphQL shape) must produce non-zero total_threads.

        Regression test: the old REST /pulls/{pr}/comments endpoint returned 0
        threads when CodeRabbit posts findings as review thread objects. This test
        uses the GraphQL reviewThreads response shape to verify total_threads > 0.
        """
        handler = HandlerCoderabbitTriage()

        graphql_threads = [
            _make_graphql_thread(
                body="_\U0001f534 Critical_ — this leaks user credentials",
                database_id=1001,
            ),
            _make_graphql_thread(
                body="_\U0001f7e1 Minor_ — consider adding a docstring",
                database_id=1002,
            ),
            _make_graphql_thread(
                body="_\U0001f9f9 Nitpick_ — trailing whitespace",
                database_id=1003,
            ),
        ]

        def mock_fetch_threads(
            owner: str, repo: str, pr_number: int
        ) -> list[dict[str, Any]]:
            return graphql_threads

        handler._fetch_review_threads = mock_fetch_threads  # type: ignore[method-assign]

        command = ModelCoderabbitTriageCommand(
            repo="OmniNode-ai/omnimarket",
            pr_number=1323,
            correlation_id="graphql-shape-test",
            dry_run=True,
        )
        result = handler.handle(command)

        assert result.total_threads == 3, (
            f"Expected 3 threads from GraphQL shape, got {result.total_threads}. "
            "Regression: REST endpoint returned 0 for this PR shape."
        )
        assert result.blocking_count >= 1
        assert result.suggestion_count >= 1

    async def test_graphql_non_coderabbit_threads_filtered(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Non-CodeRabbit authors in review threads must be filtered out."""
        handler = HandlerCoderabbitTriage()

        graphql_threads = [
            _make_graphql_thread(author_login="some-human-reviewer", body="bug here"),
            _make_graphql_thread(
                author_login="coderabbitai[bot]",
                body="nitpick: rename this var",
                database_id=999,
            ),
        ]

        def mock_fetch_threads(
            owner: str, repo: str, pr_number: int
        ) -> list[dict[str, Any]]:
            return graphql_threads

        handler._fetch_review_threads = mock_fetch_threads  # type: ignore[method-assign]

        command = ModelCoderabbitTriageCommand(
            repo="OmniNode-ai/omniclaude",
            pr_number=42,
            correlation_id="filter-test",
        )
        result = handler.handle(command)

        assert result.total_threads == 1
        assert result.suggestion_count == 1
        assert result.blocking_count == 0

    async def test_graphql_coderabbit_bot_prefix_match(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Both 'coderabbitai' and 'coderabbitai[bot]' author logins are accepted."""
        handler = HandlerCoderabbitTriage()

        graphql_threads = [
            _make_graphql_thread(
                author_login="coderabbitai", body="bug: null pointer", database_id=10
            ),
            _make_graphql_thread(
                author_login="coderabbitai[bot]",
                body="nitpick: rename",
                database_id=11,
            ),
        ]

        def mock_fetch_threads(
            owner: str, repo: str, pr_number: int
        ) -> list[dict[str, Any]]:
            return graphql_threads

        handler._fetch_review_threads = mock_fetch_threads  # type: ignore[method-assign]

        command = ModelCoderabbitTriageCommand(
            repo="OmniNode-ai/omniclaude",
            pr_number=5,
            correlation_id="prefix-match-test",
        )
        result = handler.handle(command)

        assert result.total_threads == 2

    # --- Integration tests with dry_run ---

    async def test_dry_run_no_subprocess_calls(
        self, event_bus: EventBusInmemory
    ) -> None:
        """dry_run mode with no threads returns zero counts."""
        handler = HandlerCoderabbitTriage()

        def mock_fetch_threads(
            owner: str, repo: str, pr_number: int
        ) -> list[dict[str, Any]]:
            return []

        handler._fetch_review_threads = mock_fetch_threads  # type: ignore[method-assign]

        command = ModelCoderabbitTriageCommand(
            repo="OmniNode-ai/omniclaude",
            pr_number=42,
            correlation_id="dry-test-001",
            dry_run=True,
        )
        result = handler.handle(command)

        assert result.total_threads == 0
        assert result.blocking_count == 0
        assert result.suggestion_count == 0
        assert result.dry_run is True

    async def test_blocking_detection_via_inject(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Injecting a BLOCKING thread yields blocking_count=1."""
        handler = HandlerCoderabbitTriage()

        blocking_thread = ModelThreadClassification(
            comment_id=101,
            body_excerpt="critical security issue found",
            severity=EnumThreadSeverity.BLOCKING,
            matched_keyword="critical",
            url="https://github.com/example/pr/101",
        )

        def mock_fetch(repo: str, pr_number: int) -> list[ModelThreadClassification]:
            return [blocking_thread]

        handler._fetch_and_classify = mock_fetch  # type: ignore[method-assign]

        command = ModelCoderabbitTriageCommand(
            repo="OmniNode-ai/omniclaude",
            pr_number=10,
            correlation_id="blocking-test",
            dry_run=True,
        )
        result = handler.handle(command)

        assert result.blocking_count == 1
        assert result.suggestion_count == 0
        assert result.has_blockers is True

    async def test_suggestion_only_no_blockers(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All-suggestion threads yield has_blockers=False."""
        handler = HandlerCoderabbitTriage()

        suggestion_thread = ModelThreadClassification(
            comment_id=202,
            body_excerpt="nitpick: prefer f-strings",
            severity=EnumThreadSeverity.SUGGESTION,
            matched_keyword="nitpick",
        )

        def mock_fetch(repo: str, pr_number: int) -> list[ModelThreadClassification]:
            return [suggestion_thread]

        handler._fetch_and_classify = mock_fetch  # type: ignore[method-assign]

        command = ModelCoderabbitTriageCommand(
            repo="OmniNode-ai/omniclaude",
            pr_number=7,
            correlation_id="suggestion-test",
        )
        result = handler.handle(command)

        assert result.suggestion_count == 1
        assert result.blocking_count == 0
        assert result.has_blockers is False

    async def test_repo_split_valid(self, event_bus: EventBusInmemory) -> None:
        """_split_repo correctly splits valid owner/repo strings."""
        owner, name = HandlerCoderabbitTriage._split_repo("OmniNode-ai/omniclaude")
        assert owner == "OmniNode-ai"
        assert name == "omniclaude"

    async def test_repo_split_invalid_raises(self, event_bus: EventBusInmemory) -> None:
        """_split_repo raises ValueError for malformed repo strings."""
        with pytest.raises(ValueError, match="Invalid repo format"):
            HandlerCoderabbitTriage._split_repo("no-slash-here")

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus and process command events."""
        handler = HandlerCoderabbitTriage()
        results_captured: list[dict[str, Any]] = []

        def mock_fetch_threads(
            owner: str, repo: str, pr_number: int
        ) -> list[dict[str, Any]]:
            return []

        handler._fetch_review_threads = mock_fetch_threads  # type: ignore[method-assign]

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelCoderabbitTriageCommand(**payload)
            result = handler.handle(command)
            result_payload = result.model_dump(mode="json")
            results_captured.append(result_payload)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-cr-triage"
        )

        cmd_payload = json.dumps(
            {
                "repo": "OmniNode-ai/omniclaude",
                "pr_number": 42,
                "correlation_id": "bus-test-cr",
                "dry_run": True,
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1
        assert results_captured[0]["blocking_count"] == 0

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_coderabbit_helper_login_rejected(
        self, event_bus: EventBusInmemory
    ) -> None:
        """'coderabbitai-helper' is NOT a supported CR login and must be filtered out."""
        handler = HandlerCoderabbitTriage()

        graphql_threads = [
            _make_graphql_thread(
                author_login="coderabbitai-helper", body="critical bug", database_id=20
            ),
            _make_graphql_thread(
                author_login="coderabbitai[bot]",
                body="nitpick: rename",
                database_id=21,
            ),
        ]

        def mock_fetch_threads(
            owner: str, repo: str, pr_number: int
        ) -> list[dict[str, Any]]:
            return graphql_threads

        handler._fetch_review_threads = mock_fetch_threads  # type: ignore[method-assign]

        command = ModelCoderabbitTriageCommand(
            repo="OmniNode-ai/omnimarket",
            pr_number=99,
            correlation_id="helper-reject-test",
        )
        result = handler.handle(command)

        # coderabbitai-helper must be rejected; only the [bot] thread counts
        assert result.total_threads == 1
        assert result.suggestion_count == 1
        assert result.blocking_count == 0

    async def test_pagination_nonzero_returncode_raises(
        self, event_bus: EventBusInmemory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-zero gh returncode must raise RuntimeError (fail closed)."""
        import subprocess as _subprocess

        handler = HandlerCoderabbitTriage()

        class _FakeResult:
            returncode = 1
            stderr = "authentication required"
            stdout = ""

        monkeypatch.setattr(_subprocess, "run", lambda *_a, **_kw: _FakeResult())

        with pytest.raises(RuntimeError, match="gh api graphql failed"):
            handler._fetch_review_threads("OmniNode-ai", "omnimarket", 313)

    async def test_pagination_json_decode_error_raises(
        self, event_bus: EventBusInmemory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed JSON response must raise RuntimeError (fail closed)."""
        import subprocess as _subprocess

        handler = HandlerCoderabbitTriage()

        class _FakeResult:
            returncode = 0
            stderr = ""
            stdout = "not valid json{"

        monkeypatch.setattr(_subprocess, "run", lambda *_a, **_kw: _FakeResult())

        with pytest.raises(RuntimeError, match="failed to parse gh graphql response"):
            handler._fetch_review_threads("OmniNode-ai", "omnimarket", 313)

    async def test_pagination_graphql_errors_raises(
        self, event_bus: EventBusInmemory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GraphQL-level errors in the response must raise RuntimeError (fail closed)."""
        import subprocess as _subprocess

        handler = HandlerCoderabbitTriage()

        class _FakeResult:
            returncode = 0
            stderr = ""
            stdout = json.dumps({"errors": [{"message": "rate limit exceeded"}]})

        monkeypatch.setattr(_subprocess, "run", lambda *_a, **_kw: _FakeResult())

        with pytest.raises(RuntimeError, match="GraphQL errors"):
            handler._fetch_review_threads("OmniNode-ai", "omnimarket", 313)

    async def test_result_serializes_to_json(self, event_bus: EventBusInmemory) -> None:
        """Result should serialize cleanly to JSON."""
        handler = HandlerCoderabbitTriage()

        def mock_fetch_threads(
            owner: str, repo: str, pr_number: int
        ) -> list[dict[str, Any]]:
            return []

        handler._fetch_review_threads = mock_fetch_threads  # type: ignore[method-assign]

        command = ModelCoderabbitTriageCommand(
            repo="OmniNode-ai/omniclaude",
            pr_number=3,
            correlation_id="json-test-cr",
            dry_run=True,
        )
        result = handler.handle(command)
        serialized = result.model_dump_json()
        parsed = json.loads(serialized)

        assert parsed["total_threads"] == 0
        assert parsed["blocking_count"] == 0
        assert parsed["dry_run"] is True
