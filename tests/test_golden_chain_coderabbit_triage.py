"""Golden chain test for node_coderabbit_triage.

Verifies the keyword-based classification logic and event bus wiring.
All tests that test classification use the classify_body method directly
(no subprocess calls). Event bus tests use dry_run mode.
"""

from __future__ import annotations

import json

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

    # --- Integration tests with dry_run ---

    async def test_dry_run_no_subprocess_calls(
        self, event_bus: EventBusInmemory
    ) -> None:
        """dry_run mode with no threads returns zero counts."""
        handler = HandlerCoderabbitTriage()

        # Inject a pre-classified thread list by subclassing the private method
        original = handler._fetch_and_classify

        def mock_fetch(repo: str, pr_number: int) -> list[ModelThreadClassification]:
            return []

        handler._fetch_and_classify = mock_fetch  # type: ignore[method-assign]

        command = ModelCoderabbitTriageCommand(
            repo="OmniNode-ai/omniclaude",
            pr_number=42,
            correlation_id="dry-test-001",
            dry_run=True,
        )
        result = handler.handle(command)
        handler._fetch_and_classify = original  # type: ignore[method-assign]

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
        results_captured: list[dict] = []  # type: ignore[type-arg]

        def mock_fetch(repo: str, pr_number: int) -> list[ModelThreadClassification]:
            return []

        handler._fetch_and_classify = mock_fetch  # type: ignore[method-assign]

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

    async def test_result_serializes_to_json(self, event_bus: EventBusInmemory) -> None:
        """Result should serialize cleanly to JSON."""
        handler = HandlerCoderabbitTriage()

        def mock_fetch(repo: str, pr_number: int) -> list[ModelThreadClassification]:
            return []

        handler._fetch_and_classify = mock_fetch  # type: ignore[method-assign]

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
