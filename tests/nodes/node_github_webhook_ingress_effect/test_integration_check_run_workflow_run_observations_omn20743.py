# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Signed check_run and workflow_run deliveries reach the wired handler as observations (OMN-20743)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

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
    ModelGitHubWebhookDelivery,
    ModelGitHubWorkflowRunObservation,
)

pytestmark = pytest.mark.integration

SECRET = "integration-test-webhook-secret"
RECEIVED = datetime(2026, 10, 10, 6, 0, 0, tzinfo=UTC)
REPO = {"full_name": "OmniNode-ai/omnimarket"}
RUN_ID = 17000000001


def _delivery(event: str, delivery_id: str, payload: dict[str, object]):
    body = json.dumps(payload).encode()
    return ModelGitHubWebhookDelivery(
        event=event,
        delivery_id=delivery_id,
        signature_256="sha256="
        + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
        body_b64=base64.b64encode(body).decode(),
        received_at=RECEIVED,
    )


@pytest.mark.asyncio
async def test_signed_deliveries_yield_check_run_and_workflow_run_observations() -> (
    None
):
    handler = HandlerGitHubWebhookIngress(webhook_secret=SECRET)
    check_run = await handler.handle(
        _delivery(
            "check_run",
            "72d3162e-cc78-11e3-81ab-4c9367dc0958",
            {
                "action": "completed",
                "repository": REPO,
                "check_run": {
                    "name": "unit",
                    "status": "completed",
                    "conclusion": "failure",
                    "head_sha": "a" * 40,
                    "started_at": "2026-10-10T05:50:00Z",
                    "completed_at": "2026-10-10T05:58:00Z",
                    "details_url": f"https://github.com/OmniNode-ai/omnimarket/actions/runs/{RUN_ID}/job/4",
                    "pull_requests": [{"number": 3050, "base": {"ref": "dev"}}],
                },
            },
        )
    )
    (red,) = check_run.events
    assert isinstance(red, ModelGitHubCheckRunObservation)
    assert red.topic == GITHUB_CHECK_RUN_TOPIC_V1

    workflow_run = await handler.handle(
        _delivery(
            "workflow_run",
            "72d3162e-cc78-11e3-81ab-4c9367dc0959",
            {
                "action": "completed",
                "repository": REPO,
                "workflow_run": {
                    "id": RUN_ID,
                    "name": "CI",
                    "head_sha": "a" * 40,
                    "updated_at": "2026-10-10T05:59:00Z",
                },
            },
        )
    )
    (named,) = workflow_run.events
    assert isinstance(named, ModelGitHubWorkflowRunObservation)
    assert named.topic == GITHUB_WORKFLOW_RUN_TOPIC_V1
    assert (named.run_id, named.workflow) == (RUN_ID, "CI")
