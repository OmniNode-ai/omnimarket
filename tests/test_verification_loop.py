# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for commit-citation parser and verification loop handler.

OMN-8494: Bot verification loop — commit-citation parsing + hostile_reviewer
integration + conditional resolveReviewThread mutation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_pr_review_bot.handlers.handler_verification_loop import (
    HandlerVerificationLoop,
    ProtocolGraphQLClient,
    ProtocolHostileReviewerInvoker,
    parse_commit_citations,
)
from omnimarket.nodes.node_pr_review_bot.models.models import (
    DiffHunk,
    EnumFindingCategory,
    EnumFindingSeverity,
    EnumReviewConfidence,
    PrReviewFindingEvidence,
    ReviewFinding,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_finding(
    *,
    thread_id: str | None = None,
    file_path: str = "src/foo.py",
    line_start: int = 10,
    line_end: int = 20,
) -> ReviewFinding:
    fid = UUID(thread_id) if thread_id else uuid4()
    return ReviewFinding(
        id=fid,
        category=EnumFindingCategory.LOGIC_ERROR,
        severity=EnumFindingSeverity.MAJOR,
        title="Test finding",
        description="A test finding description.",
        evidence=PrReviewFindingEvidence(
            file_path=file_path,
            line_start=line_start,
            line_end=line_end,
        ),
        confidence=EnumReviewConfidence.HIGH,
        source_model="qwen3-coder",
    )


def _make_hunk(
    file_path: str = "src/foo.py", start: int = 1, end: int = 30
) -> DiffHunk:
    return DiffHunk(
        file_path=file_path,
        start_line=start,
        end_line=end,
        content="@@ -1,10 +1,10 @@\n-old line\n+new line\n",
    )


# ---------------------------------------------------------------------------
# Citation parser tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCommitCitationParser:
    def test_commit_citation_parser_extracts_fixes_reference(self) -> None:
        """'Fixes <thread_id>' format is extracted."""
        tid = "a1b2c3d4-0000-0000-0000-000000000001"
        commit_body = f"Refactor auth module\n\nFixes {tid}"
        citations = parse_commit_citations(commit_body)
        assert len(citations) == 1
        assert citations[0].thread_id == tid

    def test_commit_citation_parser_extracts_resolves_thread(self) -> None:
        """'Resolves thread <thread_id>' format is extracted."""
        tid = "a1b2c3d4-0000-0000-0000-000000000002"
        commit_body = f"Update logic\n\nResolves thread {tid}"
        citations = parse_commit_citations(commit_body)
        assert len(citations) == 1
        assert citations[0].thread_id == tid

    def test_commit_citation_parser_ignores_prose_acknowledgment(self) -> None:
        """'will fix', 'acknowledged', 'I will address' are NOT citations."""
        prose_bodies = [
            "will fix this later",
            "acknowledged, I understand the concern",
            "I will address this in a follow-up",
            "TODO: fix this",
            "See comment above for explanation",
        ]
        for body in prose_bodies:
            citations = parse_commit_citations(body)
            assert citations == [], f"Expected no citations for: {body!r}"

    def test_commit_citation_parser_handles_multiple_citations(self) -> None:
        """Multiple citation formats in one commit are all extracted."""
        tid1 = "a1b2c3d4-0000-0000-0000-000000000001"
        tid2 = "a1b2c3d4-0000-0000-0000-000000000002"
        commit_body = f"Multi-fix\n\nFixes {tid1}\nResolves thread {tid2}"
        citations = parse_commit_citations(commit_body)
        ids = {c.thread_id for c in citations if c.thread_id}
        assert tid1 in ids
        assert tid2 in ids

    def test_commit_citation_parser_empty_body_returns_empty(self) -> None:
        """Empty commit body yields no citations."""
        assert parse_commit_citations("") == []

    def test_commit_citation_parser_no_citation_returns_empty(self) -> None:
        """A normal commit message with no citation format returns empty."""
        assert parse_commit_citations("Fix typo in README") == []


# ---------------------------------------------------------------------------
# Verification handler tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVerificationLoop:
    def test_verification_resolves_thread_on_convergent_clean_review(self) -> None:
        """When both models return CLEAN, resolveReviewThread mutation is called."""
        finding = _make_finding()
        hunk = _make_hunk(file_path="src/foo.py")

        # Both models return CLEAN
        mock_reviewer = MagicMock(spec=ProtocolHostileReviewerInvoker)
        mock_reviewer.review_hunk.return_value = {
            "verdict": "CLEAN",
            "reasoning": "Fixed.",
        }

        mock_gql = MagicMock(spec=ProtocolGraphQLClient)
        mock_gql.resolve_thread.return_value = True

        handler = HandlerVerificationLoop(
            reviewer=mock_reviewer,
            graphql_client=mock_gql,
            reviewer_models=["qwen3-coder", "deepseek-r1"],
        )

        commit_body = f"Fix null check\n\nFixes {finding.id}"
        result = handler.run(
            commit_body=commit_body,
            commit_sha="abc1234",
            pr_number=42,
            repo="owner/repo",
            open_findings=[finding],
            diff_hunks=[hunk],
        )

        assert result.resolved_thread_ids == [str(finding.id)]
        mock_gql.resolve_thread.assert_called_once_with(
            pr_number=42,
            repo="owner/repo",
            thread_id=str(finding.id),
        )

    def test_verification_posts_rejection_reply_on_dirty_review(self) -> None:
        """When a model returns DIRTY/FAIL verdict, post reply and leave thread open."""
        finding = _make_finding()
        hunk = _make_hunk()

        mock_reviewer = MagicMock(spec=ProtocolHostileReviewerInvoker)
        # First model DIRTY
        mock_reviewer.review_hunk.return_value = {
            "verdict": "DIRTY",
            "reasoning": "The fix is incomplete.",
        }

        mock_gql = MagicMock(spec=ProtocolGraphQLClient)

        handler = HandlerVerificationLoop(
            reviewer=mock_reviewer,
            graphql_client=mock_gql,
            reviewer_models=["qwen3-coder", "deepseek-r1"],
        )

        commit_body = f"Fix null check\n\nFixes {finding.id}"
        result = handler.run(
            commit_body=commit_body,
            commit_sha="abc1234",
            pr_number=42,
            repo="owner/repo",
            open_findings=[finding],
            diff_hunks=[hunk],
        )

        assert result.resolved_thread_ids == []
        mock_gql.resolve_thread.assert_not_called()
        mock_gql.post_thread_reply.assert_called_once()
        reply_text = mock_gql.post_thread_reply.call_args[1]["body"]
        assert "Verification failed" in reply_text

    def test_verification_posts_rejection_reply_on_non_convergent_review(self) -> None:
        """When models disagree (one CLEAN, one DIRTY), leave thread open with reply."""
        finding = _make_finding()
        hunk = _make_hunk()

        call_count = 0

        def alternating_review(**kwargs: Any) -> dict[str, str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"verdict": "CLEAN", "reasoning": "Looks good."}
            return {"verdict": "DIRTY", "reasoning": "Still has issues."}

        mock_reviewer = MagicMock(spec=ProtocolHostileReviewerInvoker)
        mock_reviewer.review_hunk.side_effect = alternating_review

        mock_gql = MagicMock(spec=ProtocolGraphQLClient)

        handler = HandlerVerificationLoop(
            reviewer=mock_reviewer,
            graphql_client=mock_gql,
            reviewer_models=["qwen3-coder", "deepseek-r1"],
        )

        commit_body = f"Fix null check\n\nFixes {finding.id}"
        result = handler.run(
            commit_body=commit_body,
            commit_sha="abc1234",
            pr_number=42,
            repo="owner/repo",
            open_findings=[finding],
            diff_hunks=[hunk],
        )

        assert result.resolved_thread_ids == []
        mock_gql.resolve_thread.assert_not_called()
        mock_gql.post_thread_reply.assert_called_once()

    def test_verification_never_resolves_without_a_cited_commit(self) -> None:
        """A finding with no commit citation is never resolved, even if the diff is clean."""
        finding = _make_finding()
        hunk = _make_hunk()

        mock_reviewer = MagicMock(spec=ProtocolHostileReviewerInvoker)
        mock_reviewer.review_hunk.return_value = {
            "verdict": "CLEAN",
            "reasoning": "Fixed.",
        }

        mock_gql = MagicMock(spec=ProtocolGraphQLClient)

        handler = HandlerVerificationLoop(
            reviewer=mock_reviewer,
            graphql_client=mock_gql,
            reviewer_models=["qwen3-coder", "deepseek-r1"],
        )

        # No citation in commit body
        commit_body = "Fix null check — no citation"
        result = handler.run(
            commit_body=commit_body,
            commit_sha="abc1234",
            pr_number=42,
            repo="owner/repo",
            open_findings=[finding],
            diff_hunks=[hunk],
        )

        assert result.resolved_thread_ids == []
        mock_reviewer.review_hunk.assert_not_called()
        mock_gql.resolve_thread.assert_not_called()

    def test_verification_bot_is_actor_on_resolve_thread_call(self) -> None:
        """The GraphQL client is always called with the bot as the authenticated actor."""
        finding = _make_finding()
        hunk = _make_hunk()

        mock_reviewer = MagicMock(spec=ProtocolHostileReviewerInvoker)
        mock_reviewer.review_hunk.return_value = {
            "verdict": "CLEAN",
            "reasoning": "Fixed.",
        }

        mock_gql = MagicMock(spec=ProtocolGraphQLClient)
        mock_gql.resolve_thread.return_value = True

        handler = HandlerVerificationLoop(
            reviewer=mock_reviewer,
            graphql_client=mock_gql,
            reviewer_models=["qwen3-coder", "deepseek-r1"],
        )

        commit_body = f"Fix\n\nFixes {finding.id}"
        handler.run(
            commit_body=commit_body,
            commit_sha="abc1234",
            pr_number=42,
            repo="owner/repo",
            open_findings=[finding],
            diff_hunks=[hunk],
        )

        # resolve_thread is called on the injected client (bot is the auth actor)
        mock_gql.resolve_thread.assert_called_once()
        call_kwargs = mock_gql.resolve_thread.call_args[1]
        assert call_kwargs["thread_id"] == str(finding.id)

    def test_verification_uncited_finding_skipped_no_review_call(self) -> None:
        """Findings not cited in commit are skipped — hostile_reviewer not invoked for them."""
        cited_finding = _make_finding()
        uncited_finding = _make_finding()
        cited_hunk = _make_hunk()

        mock_reviewer = MagicMock(spec=ProtocolHostileReviewerInvoker)
        mock_reviewer.review_hunk.return_value = {
            "verdict": "CLEAN",
            "reasoning": "Fixed.",
        }

        mock_gql = MagicMock(spec=ProtocolGraphQLClient)
        mock_gql.resolve_thread.return_value = True

        handler = HandlerVerificationLoop(
            reviewer=mock_reviewer,
            graphql_client=mock_gql,
            reviewer_models=["qwen3-coder", "deepseek-r1"],
        )

        commit_body = f"Fix\n\nFixes {cited_finding.id}"
        result = handler.run(
            commit_body=commit_body,
            commit_sha="abc1234",
            pr_number=42,
            repo="owner/repo",
            open_findings=[cited_finding, uncited_finding],
            diff_hunks=[cited_hunk],
        )

        # Only the cited finding is resolved
        assert result.resolved_thread_ids == [str(cited_finding.id)]
        # reviewer called exactly twice (2 models * 1 cited finding)
        assert mock_reviewer.review_hunk.call_count == 2


# ---------------------------------------------------------------------------
# Finding 4: empty reviewer_models must fail closed (not silently pass)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmptyReviewerModels:
    def test_empty_reviewer_models_raises_on_init(self) -> None:
        """Empty reviewer_models must raise ValueError — fail closed, never open."""
        mock_reviewer = MagicMock(spec=ProtocolHostileReviewerInvoker)
        mock_gql = MagicMock(spec=ProtocolGraphQLClient)

        with pytest.raises(ValueError, match="reviewer_models"):
            HandlerVerificationLoop(
                reviewer=mock_reviewer,
                graphql_client=mock_gql,
                reviewer_models=[],
            )

    def test_empty_reviewer_models_never_resolves_thread(self) -> None:
        """all([]) == True is the silent bypass — verify the guard prevents it."""
        mock_reviewer = MagicMock(spec=ProtocolHostileReviewerInvoker)
        mock_gql = MagicMock(spec=ProtocolGraphQLClient)

        try:
            handler = HandlerVerificationLoop(
                reviewer=mock_reviewer,
                graphql_client=mock_gql,
                reviewer_models=[],
            )
        except ValueError:
            return  # correct — guard fired

        # If construction succeeded, run() must not resolve anything
        finding = _make_finding()
        commit_body = f"Fix\n\nFixes {finding.id}"
        result = handler.run(
            commit_body=commit_body,
            commit_sha="abc1234",
            pr_number=1,
            repo="owner/repo",
            open_findings=[finding],
            diff_hunks=[_make_hunk()],
        )
        assert result.resolved_thread_ids == []
        mock_gql.resolve_thread.assert_not_called()


# ---------------------------------------------------------------------------
# Finding 5: commit_sha / finding_position citation forms deleted (dead code)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeadCitationFormsDeleted:
    def test_parse_commit_citations_does_not_return_commit_sha(self) -> None:
        """commit_sha citation form must be removed — dead code deleted."""
        sha = "abc1234def5678"
        commit_body = f"Fix\n\nAddressed in commit {sha}"
        citations = parse_commit_citations(commit_body)
        # After deletion: no citation with commit_sha should appear
        assert all(c.commit_sha is None for c in citations), (
            "commit_sha citation form is dead code and must be deleted"
        )

    def test_parse_commit_citations_does_not_return_finding_position(self) -> None:
        """finding_position citation form must be removed — dead code deleted."""
        commit_body = "Fix validation\n\nResolves: #findings[3]"
        citations = parse_commit_citations(commit_body)
        # After deletion: no citation with finding_position should appear
        assert all(c.finding_position is None for c in citations), (
            "finding_position citation form is dead code and must be deleted"
        )


# ---------------------------------------------------------------------------
# Finding 6: empty diff scope must fail closed (no hostile_reviewer call)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmptyDiffScopeFailClosed:
    def test_empty_diff_scope_does_not_invoke_reviewer(self) -> None:
        """When _scope_hunk returns None, hostile_reviewer must NOT be called."""
        # finding references a file not in diff_hunks → scope is None
        finding = _make_finding(file_path="src/missing.py")
        hunk = _make_hunk(file_path="src/other.py")  # different file

        mock_reviewer = MagicMock(spec=ProtocolHostileReviewerInvoker)
        mock_gql = MagicMock(spec=ProtocolGraphQLClient)

        handler = HandlerVerificationLoop(
            reviewer=mock_reviewer,
            graphql_client=mock_gql,
            reviewer_models=["qwen3-coder"],
        )

        commit_body = f"Fix\n\nFixes {finding.id}"
        result = handler.run(
            commit_body=commit_body,
            commit_sha="abc1234",
            pr_number=1,
            repo="owner/repo",
            open_findings=[finding],
            diff_hunks=[hunk],
        )

        mock_reviewer.review_hunk.assert_not_called()
        assert str(finding.id) not in result.resolved_thread_ids
        assert str(finding.id) in result.rejected_thread_ids


# ---------------------------------------------------------------------------
# Finding 7: resolve_thread() return value must be honored
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveThreadReturnValueHonored:
    def test_failed_resolve_thread_mutation_not_logged_as_resolved(self) -> None:
        """resolve_thread() returning False must NOT add fid to resolved_thread_ids."""
        finding = _make_finding()
        hunk = _make_hunk()

        mock_reviewer = MagicMock(spec=ProtocolHostileReviewerInvoker)
        mock_reviewer.review_hunk.return_value = {"verdict": "CLEAN", "reasoning": "ok"}

        mock_gql = MagicMock(spec=ProtocolGraphQLClient)
        mock_gql.resolve_thread.return_value = False  # mutation failed silently

        handler = HandlerVerificationLoop(
            reviewer=mock_reviewer,
            graphql_client=mock_gql,
            reviewer_models=["qwen3-coder"],
        )

        commit_body = f"Fix\n\nFixes {finding.id}"
        result = handler.run(
            commit_body=commit_body,
            commit_sha="abc1234",
            pr_number=1,
            repo="owner/repo",
            open_findings=[finding],
            diff_hunks=[hunk],
        )

        assert str(finding.id) not in result.resolved_thread_ids
        assert str(finding.id) in result.rejected_thread_ids


# ---------------------------------------------------------------------------
# Finding 3: contract.yaml must parse without YAML errors
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContractYamlValid:
    def test_contract_yaml_loads_without_duplicate_key_error(self) -> None:
        """contract.yaml must be loadable — duplicate metadata.updated key is a parse error."""
        from pathlib import Path

        import yaml

        contract_path = (
            Path(__file__).parent.parent
            / "src/omnimarket/nodes/node_pr_review_bot/contract.yaml"
        )
        with open(contract_path) as f:
            content = f.read()

        # yaml.safe_load raises on duplicate keys with strict loaders;
        # use a custom loader that raises on duplicates
        class StrictLoader(yaml.SafeLoader):
            pass

        def _construct_mapping(loader: yaml.Loader, node: yaml.MappingNode) -> dict:
            loader.flatten_mapping(node)
            pairs = loader.construct_pairs(node)
            keys = [k for k, _ in pairs]
            seen: set = set()
            for k in keys:
                if k in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"Duplicate key: {k!r}", node.start_mark
                    )
                seen.add(k)
            return dict(pairs)

        StrictLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
            _construct_mapping,
        )
        # Must not raise
        yaml.load(content, Loader=StrictLoader)
