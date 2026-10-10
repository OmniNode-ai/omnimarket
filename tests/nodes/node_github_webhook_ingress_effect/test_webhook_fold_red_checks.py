# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Any red check run, and the workflow runs that name them, fold into observations (OMN-20743)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from omnimarket.events.topics import (
    GITHUB_CHECK_RUN_TOPIC_V1,
    GITHUB_WORKFLOW_RUN_TOPIC_V1,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.handlers import (
    HandlerGitHubWebhookIngress,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.models import (
    ModelGitHubCheckRunObservation,
    ModelGitHubPrStateObservation,
    ModelGitHubWebhookDelivery,
    ModelGitHubWorkflowRunObservation,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.webhook_fold import (
    WebhookFoldError,
    fold_delivery,
)

pytestmark = pytest.mark.unit

RECEIVED = datetime(2026, 10, 10, 6, 0, 0, tzinfo=UTC)
D1 = UUID("72d3162e-cc78-11e3-81ab-4c9367dc0958")
REPO = {"full_name": "OmniNode-ai/omnimarket"}
SUMMARY = frozenset({"CI Summary"})


def _run(**overrides: object) -> dict[str, object]:
    run: dict[str, object] = {
        "name": "unit",
        "status": "completed",
        "conclusion": "failure",
        "head_sha": "a" * 40,
        "started_at": "2026-10-10T05:50:00Z",
        "completed_at": "2026-10-10T05:58:00Z",
        "details_url": "https://github.com/OmniNode-ai/omnimarket/actions/runs/17000000001/job/4",
        "pull_requests": [
            {"number": 3050, "base": {"ref": "dev"}},
            {"number": 3051, "base": {"ref": "dev"}},
        ],
    }
    run.update(overrides)
    return run


def _fold(event: str, payload: dict[str, object]) -> tuple[object, ...]:
    return fold_delivery(
        event=event,
        delivery_id=D1,
        payload=payload,
        received_at=RECEIVED,
        published_at=RECEIVED,
        summary_check_names=SUMMARY,
    )


def _check_run(**overrides: object) -> tuple[object, ...]:
    return _fold(
        "check_run",
        {"action": "completed", "check_run": _run(**overrides), "repository": REPO},
    )


class TestRedCheckRun:
    def test_a_red_non_summary_check_yields_one_observation_per_pr(self) -> None:
        observations = _check_run()
        assert [type(o) for o in observations] == [ModelGitHubCheckRunObservation] * 2
        first = observations[0]
        assert isinstance(first, ModelGitHubCheckRunObservation)
        assert first.topic == GITHUB_CHECK_RUN_TOPIC_V1
        assert (first.repo, first.pr_number) == ("OmniNode-ai/omnimarket", 3050)
        assert (first.check, first.conclusion, first.run_id) == (
            "unit",
            "failure",
            17000000001,
        )
        assert first.head_sha == "a" * 40
        assert first.base_ref == "dev"
        assert first.completed_at == datetime(2026, 10, 10, 5, 58, tzinfo=UTC)
        wire = first.model_dump(mode="json")
        assert wire["completed_at"] == "2026-10-10T05:58:00Z"

    @pytest.mark.parametrize(
        "conclusion",
        ["failure", "timed_out", "action_required", "startup_failure", "stale"],
    )
    def test_every_red_conclusion_is_published(self, conclusion: str) -> None:
        (first, _) = _check_run(conclusion=conclusion)
        assert isinstance(first, ModelGitHubCheckRunObservation)
        assert first.conclusion == conclusion

    @pytest.mark.parametrize(
        "conclusion", ["success", "neutral", "skipped", "cancelled"]
    )
    def test_passing_and_no_verdict_conclusions_say_nothing(
        self, conclusion: str
    ) -> None:
        assert _check_run(conclusion=conclusion) == ()

    def test_a_run_still_in_progress_says_nothing(self) -> None:
        assert (
            _check_run(status="in_progress", conclusion=None, completed_at=None) == ()
        )

    def test_a_check_another_app_posted_has_no_run_id(self) -> None:
        (first, _) = _check_run(
            name="CodeRabbit", details_url="https://coderabbit.ai/review/9"
        )
        assert isinstance(first, ModelGitHubCheckRunObservation)
        assert first.run_id is None

    def test_a_run_naming_no_pr_says_nothing(self) -> None:
        assert _check_run(pull_requests=[]) == ()

    def test_a_red_summary_check_yields_the_check_run_and_the_ci_status(self) -> None:
        observations = _check_run(name="CI Summary", pull_requests=[{"number": 3050}])
        assert [type(o) for o in observations] == [
            ModelGitHubCheckRunObservation,
            ModelGitHubPrStateObservation,
        ]
        status = observations[1]
        assert isinstance(status, ModelGitHubPrStateObservation)
        assert status.ci_status == "FAILURE"

    def test_a_passing_summary_check_is_unchanged(self) -> None:
        (only,) = _check_run(
            name="CI Summary", conclusion="success", pull_requests=[{"number": 3050}]
        )
        assert isinstance(only, ModelGitHubPrStateObservation)
        assert only.ci_status == "SUCCESS"

    def test_a_red_check_without_a_head_sha_is_refused(self) -> None:
        with pytest.raises(WebhookFoldError, match="head_sha"):
            _check_run(head_sha=None)

    def test_a_malformed_repository_is_ignored_for_a_check_that_says_nothing(
        self,
    ) -> None:
        assert (
            _fold(
                "check_run",
                {"action": "completed", "check_run": _run(conclusion="success")},
            )
            == ()
        )


class TestWorkflowRun:
    def _payload(self, action: str = "requested", **run: object) -> dict[str, object]:
        workflow_run: dict[str, object] = {
            "id": 17000000001,
            "name": "CI",
            "head_sha": "a" * 40,
            "updated_at": "2026-10-10T05:49:00Z",
        }
        workflow_run.update(run)
        return {"action": action, "workflow_run": workflow_run, "repository": REPO}

    @pytest.mark.parametrize("action", ["requested", "in_progress", "completed"])
    def test_names_the_workflow_of_a_run_id(self, action: str) -> None:
        (obs,) = _fold("workflow_run", self._payload(action))
        assert isinstance(obs, ModelGitHubWorkflowRunObservation)
        assert obs.topic == GITHUB_WORKFLOW_RUN_TOPIC_V1
        assert (obs.repo, obs.run_id, obs.workflow, obs.status) == (
            "OmniNode-ai/omnimarket",
            17000000001,
            "CI",
            action,
        )

    def test_an_unknown_action_or_a_nameless_run_says_nothing(self) -> None:
        assert _fold("workflow_run", self._payload("rerequested")) == ()
        assert _fold("workflow_run", self._payload(name=None)) == ()

    def test_a_run_without_an_id_is_refused(self) -> None:
        with pytest.raises(WebhookFoldError, match=r"workflow_run\.id"):
            _fold("workflow_run", self._payload(id=None))


SECRET = "unit-test-webhook-secret"


@pytest.mark.asyncio
async def test_the_wired_handler_publishes_a_verified_red_check_run() -> None:
    body = json.dumps(
        {"action": "completed", "check_run": _run(), "repository": REPO}
    ).encode()
    delivery = ModelGitHubWebhookDelivery(
        event="check_run",
        delivery_id=str(D1),
        signature_256="sha256="
        + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
        body_b64=base64.b64encode(body).decode(),
        received_at=RECEIVED,
    )
    out = await HandlerGitHubWebhookIngress(webhook_secret=SECRET).handle(delivery)
    assert [type(e) for e in out.events] == [ModelGitHubCheckRunObservation] * 2
