# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Signature check and delivery fold of the GitHub webhook ingress (OMN-19492)."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from uuid import UUID

import pytest

from omnimarket.models.model_github_pr_state_observation import (
    ModelGitHubPrStateObservation,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.models import (
    ModelGitHubBranchHeadObservation,
    ModelGitHubPrMergedObservation,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.webhook_fold import (
    WebhookFoldError,
    fold_delivery,
    verify_signature,
)

pytestmark = pytest.mark.unit

RECEIVED = datetime(2026, 9, 27, 23, 0, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 9, 27, 23, 0, 1, tzinfo=UTC)
SUMMARY = frozenset({"CI Summary"})
D1 = UUID("72d3162e-cc78-11e3-81ab-4c9367dc0958")
D2 = UUID("72d3162e-cc78-11e3-81ab-4c9367dc0959")
REPO = {"full_name": "OmniNode-ai/omnibase_infra"}


def _pr(**overrides: object) -> dict[str, object]:
    pr: dict[str, object] = {
        "number": 4242,
        "state": "open",
        "draft": False,
        "merged": False,
        "title": "feat(OMN-19492): webhook ingress",
        "updated_at": "2026-09-27T22:59:00Z",
        "closed_at": None,
        "merged_at": None,
        "merge_commit_sha": None,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"ref": "jonah/omn-14375-github-webhook-ingress", "sha": "a" * 40},
        "base": {"ref": "dev", "sha": "b" * 40},
    }
    pr.update(overrides)
    return pr


def _fold(event: str, payload: dict[str, object]) -> tuple[object, ...]:
    return fold_delivery(
        event=event,
        delivery_id=D1,
        payload=payload,
        received_at=RECEIVED,
        published_at=PUBLISHED,
        summary_check_names=SUMMARY,
    )


class TestVerifySignature:
    def test_accepts_githubs_hmac(self) -> None:
        body = b'{"zen":"Keep it logically awesome."}'
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert verify_signature(b"s3cret", body, sig) is True

    def test_rejects_a_changed_byte(self) -> None:
        body = b'{"zen":"Keep it logically awesome."}'
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert verify_signature(b"s3cret", body + b" ", sig) is False

    def test_rejects_the_wrong_secret(self) -> None:
        body = b"{}"
        sig = "sha256=" + hmac.new(b"other", body, hashlib.sha256).hexdigest()
        assert verify_signature(b"s3cret", body, sig) is False

    def test_empty_secret_verifies_nothing(self) -> None:
        body = b"{}"
        sig = "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest()
        assert verify_signature(b"", body, sig) is False

    def test_rejects_the_sha1_header_shape(self) -> None:
        body = b"{}"
        digest = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert verify_signature(b"s3cret", body, "sha1=" + digest) is False


class TestPullRequest:
    def test_synchronize_resets_ci_and_carries_refs(self) -> None:
        (obs,) = _fold(
            "pull_request",
            {"action": "synchronize", "pull_request": _pr(), "repository": REPO},
        )
        assert isinstance(obs, ModelGitHubPrStateObservation)
        assert obs.entity_id == "OmniNode-ai/omnibase_infra#4242"
        assert obs.ci_status == "PENDING"
        assert obs.triage_state is None
        assert obs.mergeable == "MERGEABLE"
        assert obs.merge_state_status == "CLEAN"
        assert (obs.base_ref, obs.head_ref, obs.head_sha) == (
            "dev",
            "jonah/omn-14375-github-webhook-ingress",
            "a" * 40,
        )
        assert obs.as_of == datetime(2026, 9, 27, 22, 59, tzinfo=UTC)

    def test_opened_non_draft_needs_review(self) -> None:
        (obs,) = _fold(
            "pull_request",
            {"action": "opened", "pull_request": _pr(), "repository": REPO},
        )
        assert isinstance(obs, ModelGitHubPrStateObservation)
        assert obs.triage_state == "needs_review"

    def test_draft_is_draft(self) -> None:
        (obs,) = _fold(
            "pull_request",
            {
                "action": "converted_to_draft",
                "pull_request": _pr(draft=True),
                "repository": REPO,
            },
        )
        assert isinstance(obs, ModelGitHubPrStateObservation)
        assert (obs.triage_state, obs.is_draft, obs.ci_status) == ("draft", True, None)

    def test_unknown_mergeability_says_nothing(self) -> None:
        (obs,) = _fold(
            "pull_request",
            {
                "action": "edited",
                "pull_request": _pr(mergeable=None, mergeable_state="unknown"),
                "repository": REPO,
            },
        )
        assert isinstance(obs, ModelGitHubPrStateObservation)
        assert (obs.mergeable, obs.merge_state_status) == (None, None)

    @pytest.mark.parametrize(
        ("action", "queue"),
        [
            ("enqueued", "QUEUED"),
            ("dequeued", "DEQUEUED"),
            ("auto_merge_enabled", "AUTO_MERGE_ARMED"),
            ("auto_merge_disabled", "AUTO_MERGE_DISARMED"),
        ],
    )
    def test_queue_actions(self, action: str, queue: str) -> None:
        (obs,) = _fold(
            "pull_request",
            {"action": action, "pull_request": _pr(), "repository": REPO},
        )
        assert isinstance(obs, ModelGitHubPrStateObservation)
        assert obs.merge_queue_state == queue

    def test_merged_close_emits_a_merge_event_for_any_repo(self) -> None:
        pr = _pr(
            state="closed",
            merged=True,
            closed_at="2026-09-27T22:59:30Z",
            merged_at="2026-09-27T22:59:30Z",
            merge_commit_sha="c" * 40,
        )
        state, merged = _fold(
            "pull_request",
            {"action": "closed", "pull_request": pr, "repository": REPO},
        )
        assert isinstance(state, ModelGitHubPrStateObservation)
        assert (state.triage_state, state.merge_queue_state, state.ci_status) == (
            "merged",
            "MERGED",
            None,
        )
        assert isinstance(merged, ModelGitHubPrMergedObservation)
        assert merged.repo == "OmniNode-ai/omnibase_infra"
        assert merged.branch == "jonah/omn-14375-github-webhook-ingress"
        assert merged.ticket == "OMN-14375"
        assert merged.merged_at == "2026-09-27T22:59:30Z"
        assert merged.topic == "onex.evt.github.pr-merged.v1"

    def test_merge_event_id_is_stable_across_redeliveries(self) -> None:
        pr = _pr(
            state="closed",
            merged=True,
            merged_at="2026-09-27T22:59:30Z",
            merge_commit_sha="c" * 40,
        )
        payload = {"action": "closed", "pull_request": pr, "repository": REPO}
        first = _fold("pull_request", payload)[1]
        second = fold_delivery(
            event="pull_request",
            delivery_id=D2,
            payload=payload,
            received_at=RECEIVED,
            published_at=PUBLISHED,
            summary_check_names=SUMMARY,
        )[1]
        assert isinstance(first, ModelGitHubPrMergedObservation)
        assert isinstance(second, ModelGitHubPrMergedObservation)
        assert first.event_id == second.event_id

    def test_ticket_falls_back_to_the_title(self) -> None:
        pr = _pr(
            state="closed",
            merged=True,
            merged_at="2026-09-27T22:59:30Z",
            merge_commit_sha="c" * 40,
            head={"ref": "fix-things", "sha": "a" * 40},
            title="fix(omn-19492): lower-case ticket",
        )
        merged = _fold(
            "pull_request",
            {"action": "closed", "pull_request": pr, "repository": REPO},
        )[1]
        assert isinstance(merged, ModelGitHubPrMergedObservation)
        assert merged.ticket == "OMN-19492"

    def test_unmerged_close_emits_no_merge_event(self) -> None:
        obs = _fold(
            "pull_request",
            {
                "action": "closed",
                "pull_request": _pr(state="closed", closed_at="2026-09-27T23:00:00Z"),
                "repository": REPO,
            },
        )
        assert len(obs) == 1
        assert isinstance(obs[0], ModelGitHubPrStateObservation)
        assert obs[0].triage_state == "closed"

    def test_merged_close_without_merge_sha_is_refused(self) -> None:
        pr = _pr(state="closed", merged=True, merged_at="2026-09-27T22:59:30Z")
        with pytest.raises(WebhookFoldError, match="merge_commit_sha"):
            _fold(
                "pull_request",
                {"action": "closed", "pull_request": pr, "repository": REPO},
            )


class TestReview:
    @pytest.mark.parametrize(
        ("action", "state", "decision"),
        [
            ("submitted", "approved", "APPROVED"),
            ("submitted", "changes_requested", "CHANGES_REQUESTED"),
            ("dismissed", "dismissed", "REVIEW_REQUIRED"),
        ],
    )
    def test_decisions(self, action: str, state: str, decision: str) -> None:
        (obs,) = _fold(
            "pull_request_review",
            {
                "action": action,
                "review": {"state": state, "submitted_at": "2026-09-27T22:58:00Z"},
                "pull_request": _pr(),
                "repository": REPO,
            },
        )
        assert isinstance(obs, ModelGitHubPrStateObservation)
        assert obs.review_decision == decision
        assert obs.title is None  # a review observation says nothing about it

    def test_a_comment_says_nothing(self) -> None:
        assert (
            _fold(
                "pull_request_review",
                {
                    "action": "submitted",
                    "review": {"state": "commented"},
                    "pull_request": _pr(),
                    "repository": REPO,
                },
            )
            == ()
        )


class TestCheckRun:
    def _run(self, **overrides: object) -> dict[str, object]:
        run: dict[str, object] = {
            "name": "CI Summary",
            "status": "completed",
            "conclusion": "success",
            "head_sha": "a" * 40,
            "started_at": "2026-09-27T22:50:00Z",
            "completed_at": "2026-09-27T22:58:00Z",
            "pull_requests": [{"number": 4242}, {"number": 4243}],
        }
        run.update(overrides)
        return run

    def test_summary_check_sets_ci_status_for_each_pr(self) -> None:
        obs = _fold(
            "check_run",
            {"action": "completed", "check_run": self._run(), "repository": REPO},
        )
        states = [o for o in obs if isinstance(o, ModelGitHubPrStateObservation)]
        assert len(states) == len(obs)
        assert [o.pr_number for o in states] == [4242, 4243]
        assert {o.ci_status for o in states} == {"SUCCESS"}

    def test_in_progress_summary_is_pending(self) -> None:
        (first, _) = _fold(
            "check_run",
            {
                "action": "created",
                "check_run": self._run(status="in_progress", conclusion=None),
                "repository": REPO,
            },
        )
        assert isinstance(first, ModelGitHubPrStateObservation)
        assert first.ci_status == "PENDING"

    def test_other_checks_say_nothing(self) -> None:
        assert (
            _fold(
                "check_run",
                {
                    "action": "completed",
                    "check_run": self._run(name="ruff"),
                    "repository": REPO,
                },
            )
            == ()
        )


def test_unhandled_events_fold_to_nothing() -> None:
    assert _fold("ping", {"zen": "Speak like a human."}) == ()
    assert _fold("push", {"ref": "refs/heads/dev", "repository": REPO}) == ()


def test_malformed_pull_request_is_refused() -> None:
    with pytest.raises(WebhookFoldError, match="repository"):
        _fold("pull_request", {"action": "opened", "pull_request": _pr()})


# --- OMN-19932 (B1): branch-head and merge-group observations -----------------

WATCHED = frozenset({"dev", "main"})
SHA_NEW = "c" * 40
SHA_OLD = "d" * 40


def _fold_branch(event: str, payload: dict[str, object]) -> tuple[object, ...]:
    return fold_delivery(
        event=event,
        delivery_id=D1,
        payload=payload,
        received_at=RECEIVED,
        published_at=PUBLISHED,
        summary_check_names=SUMMARY,
        watched_branches=WATCHED,
    )


def _summary_run(head_branch: str, *, conclusion: str | None = "success") -> dict:
    return {
        "repository": REPO,
        "check_run": {
            "name": "CI Summary",
            "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion,
            "head_sha": SHA_NEW,
            "completed_at": "2026-09-28T12:00:00Z",
            "pull_requests": [],
            "check_suite": {"head_branch": head_branch, "head_sha": SHA_NEW},
        },
    }


class TestBranchHeadObservations:
    def test_push_on_a_watched_branch_is_branch_ref_advanced(self) -> None:
        (obs,) = _fold_branch(
            "push",
            {
                "repository": REPO,
                "ref": "refs/heads/dev",
                "before": SHA_OLD,
                "after": SHA_NEW,
                "deleted": False,
            },
        )
        assert isinstance(obs, ModelGitHubBranchHeadObservation)
        assert obs.kind == "branch-ref-advanced"
        assert (obs.repo, obs.branch, obs.sha, obs.before_sha) == (
            "OmniNode-ai/omnibase_infra",
            "dev",
            SHA_NEW,
            SHA_OLD,
        )
        assert obs.entity_id == "OmniNode-ai/omnibase_infra@dev"

    def test_summary_check_with_empty_pr_list_is_branch_head_status(self) -> None:
        (obs,) = _fold_branch("check_run", _summary_run("dev"))
        assert isinstance(obs, ModelGitHubBranchHeadObservation)
        assert obs.kind == "branch-head-status"
        assert (obs.branch, obs.sha, obs.ci_status) == ("dev", SHA_NEW, "SUCCESS")

    def test_failed_merge_group_candidate_never_is_a_branch_head_event(self) -> None:
        out = _fold_branch(
            "check_run",
            _summary_run(
                "gh-readonly-queue/dev/pr-4242-" + SHA_NEW, conclusion="failure"
            ),
        )
        (obs,) = out
        assert isinstance(obs, ModelGitHubBranchHeadObservation)
        assert obs.kind == "merge-group-status"
        assert (obs.branch, obs.sha, obs.ci_status) == ("dev", SHA_NEW, "FAILURE")
        assert obs.merge_group_ref == "gh-readonly-queue/dev/pr-4242-" + SHA_NEW

    def test_unwatched_branch_folds_to_nothing(self) -> None:
        assert _fold_branch("check_run", _summary_run("feature/x")) == ()
        assert (
            _fold_branch(
                "push",
                {
                    "repository": REPO,
                    "ref": "refs/heads/feature/x",
                    "before": SHA_OLD,
                    "after": SHA_NEW,
                },
            )
            == ()
        )
        assert (
            _fold_branch(
                "check_run",
                _summary_run("gh-readonly-queue/feature/x/pr-1-" + SHA_NEW),
            )
            == ()
        )

    def test_branch_deletion_and_non_summary_checks_say_nothing(self) -> None:
        assert (
            _fold_branch(
                "push",
                {
                    "repository": REPO,
                    "ref": "refs/heads/dev",
                    "before": SHA_OLD,
                    "after": "0" * 40,
                    "deleted": True,
                },
            )
            == ()
        )
        run = _summary_run("dev")
        check_run = run["check_run"]
        assert isinstance(check_run, dict)
        check_run["name"] = "lint"
        assert _fold_branch("check_run", run) == ()

    def test_no_watched_branches_means_no_branch_events(self) -> None:
        assert _fold("check_run", _summary_run("dev")) == ()
