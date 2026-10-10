# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prove webhook contract discovery, resolver wiring, verification and folding.

Uses real contracts, a rendered resolver config on disk and an environment-backed
SecretResolver. Deliveries stay in process; no GitHub, Kafka or database is needed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
from omnibase_infra.errors import RuntimeHostError
from omnibase_infra.runtime.auto_wiring import (
    discover_contracts_from_paths,
    filter_manifest_for_runtime_profile,
)
from omnibase_infra.runtime.models.model_secret_resolver_config import (
    ModelSecretResolverConfig,
)
from omnibase_infra.runtime.secret_resolver import SecretResolver

from omnimarket.nodes.node_github_webhook_ingress_effect.handlers import (
    HandlerGitHubWebhookIngress,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.handlers import (
    handler_github_webhook_ingress as ingress_module,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.handlers.handler_github_webhook_ingress import (
    WEBHOOK_SECRET_REF,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.models import (
    ModelGitHubBranchHeadObservation,
    ModelGitHubPrMergedObservation,
    ModelGitHubPrStateObservation,
    ModelGitHubWebhookDelivery,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.webhook_fold import (
    fold_delivery,
)

pytestmark = pytest.mark.integration

_TEST_SECRET = "integration-webhook-signing-key"


def _secret_resolver() -> SecretResolver:
    """The resolver the runtime builds for a lane that maps the webhook secret."""
    return SecretResolver(
        config=ModelSecretResolverConfig.model_validate(
            {
                "enable_convention_fallback": False,
                "mappings": [
                    {
                        "logical_name": WEBHOOK_SECRET_REF,
                        "source": {
                            "source_type": "env",
                            "source_path": "GITHUB_WEBHOOK_SECRET",
                        },
                    }
                ],
            }
        )
    )


@pytest.fixture
def handler(monkeypatch: pytest.MonkeyPatch) -> HandlerGitHubWebhookIngress:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _TEST_SECRET)
    return HandlerGitHubWebhookIngress(secret_resolver=_secret_resolver())


@pytest.fixture
def delivery() -> ModelGitHubWebhookDelivery:
    body = json.dumps(
        {
            "action": "closed",
            "repository": {"full_name": "OmniNode-ai/omnibase_infra"},
            "pull_request": {
                "number": 4227,
                "state": "closed",
                "merged": True,
                "draft": False,
                "title": "feat(OMN-14375): GitHub webhook ingress",
                "closed_at": "2026-09-28T12:00:00Z",
                "merged_at": "2026-09-28T12:00:00Z",
                "merge_commit_sha": "c" * 40,
                "head": {
                    "ref": "jonah/omn-14375-github-webhook-ingress",
                    "sha": "a" * 40,
                },
                "base": {"ref": "dev"},
            },
        }
    ).encode()
    signature = hmac.new(_TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return ModelGitHubWebhookDelivery.model_validate(
        {
            "event": "pull_request",
            "delivery_id": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
            "signature_256": f"sha256={signature}",
            "body_b64": base64.b64encode(body).decode(),
            "received_at": "2026-09-28T12:00:01Z",
        }
    )


def test_contract_is_discovered_for_effects_runtime() -> None:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_github_webhook_ingress_effect/contract.yaml"
    )
    manifest = discover_contracts_from_paths([contract_path])
    assert manifest.total_errors == 0
    assert manifest.total_discovered == 1
    effects = filter_manifest_for_runtime_profile(manifest, "effects")
    assert [contract.name for contract in effects.manifest.contracts] == [
        "node_github_webhook_ingress_effect"
    ]
    assert (
        filter_manifest_for_runtime_profile(manifest, "main").manifest.contracts == ()
    )


@pytest.mark.asyncio
async def test_wired_handler_verifies_and_folds_merge(
    handler: HandlerGitHubWebhookIngress, delivery: ModelGitHubWebhookDelivery
) -> None:
    result = await handler.handle(delivery)
    assert len(result.events) == 2
    state, merged = result.events
    assert isinstance(state, ModelGitHubPrStateObservation)
    assert isinstance(merged, ModelGitHubPrMergedObservation)
    assert state.topic == "onex.evt.github.pr-status.v1"
    assert state.entity_id == "OmniNode-ai/omnibase_infra#4227"
    assert state.repo == "OmniNode-ai/omnibase_infra"
    assert state.pr_number == 4227
    assert state.delivery_id == delivery.delivery_id
    assert state.source == "webhook"
    assert state.github_event == "pull_request"
    assert state.as_of == datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    assert state.triage_state == "merged"
    assert state.merge_queue_state == "MERGED"
    assert state.head_sha == "a" * 40
    assert state.head_ref == "jonah/omn-14375-github-webhook-ingress"
    assert state.base_ref == "dev"
    assert state.title == "feat(OMN-14375): GitHub webhook ingress"
    assert state.is_draft is False
    assert state.ci_status is None
    assert state.review_decision is None
    assert merged.topic == "onex.evt.github.pr-merged.v1"
    assert merged.entity_id == state.entity_id
    assert merged.repo == state.repo
    assert merged.pr_number == state.pr_number
    assert merged.branch == state.head_ref
    assert merged.base_ref == "dev"
    assert merged.ticket == "OMN-14375"
    assert merged.merge_sha == "c" * 40
    assert merged.merged_at == "2026-09-28T12:00:00Z"
    assert isinstance(merged.event_id, UUID)
    assert merged.event_id.version == 5

    replay = await handler.handle(delivery)
    assert replay.events[0] == state
    replay_merged = replay.events[1]
    assert isinstance(replay_merged, ModelGitHubPrMergedObservation)
    assert replay_merged.event_id == merged.event_id


@pytest.mark.asyncio
async def test_wired_handler_refuses_forged_delivery_before_folding(
    handler: HandlerGitHubWebhookIngress, delivery: ModelGitHubWebhookDelivery
) -> None:
    forged = delivery.model_copy(update={"signature_256": "sha256=" + "0" * 64})
    with patch.object(ingress_module, "fold_delivery", wraps=fold_delivery) as fold:
        with pytest.raises(RuntimeHostError, match="signature does not verify"):
            await handler.handle(forged)
        fold.assert_not_called()


@pytest.mark.asyncio
async def test_a_handler_with_no_resolver_refuses_before_folding(
    monkeypatch: pytest.MonkeyPatch, delivery: ModelGitHubWebhookDelivery
) -> None:
    # Even an ambient secret must not open a lane with no explicit mapping: the
    # runtime hands the handler no resolver there, and it refuses every delivery.
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _TEST_SECRET)
    handler = HandlerGitHubWebhookIngress()
    with patch.object(ingress_module, "fold_delivery", wraps=fold_delivery) as fold:
        with pytest.raises(RuntimeHostError, match="no webhook secret configured"):
            await handler.handle(delivery)
        fold.assert_not_called()


def _signed_delivery(
    event: str, payload: dict[str, object], delivery_id: str
) -> ModelGitHubWebhookDelivery:
    body = json.dumps(payload).encode()
    signature = hmac.new(_TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return ModelGitHubWebhookDelivery.model_validate(
        {
            "event": event,
            "delivery_id": delivery_id,
            "signature_256": f"sha256={signature}",
            "body_b64": base64.b64encode(body).decode(),
            "received_at": "2026-09-29T12:00:01Z",
        }
    )


def _summary_check_run(head_branch: str) -> dict[str, object]:
    return {
        "action": "completed",
        "repository": {"full_name": "OmniNode-ai/omnibase_infra"},
        "check_run": {
            "name": "CI Summary",
            "status": "completed",
            "conclusion": "success",
            "head_sha": "d" * 40,
            "completed_at": "2026-09-29T12:00:00Z",
            "pull_requests": [],
            "check_suite": {"head_branch": head_branch},
        },
    }


@pytest.mark.asyncio
async def test_wired_handler_folds_a_watched_branch_push_to_a_ref_advance(
    handler: HandlerGitHubWebhookIngress,
) -> None:
    delivery = _signed_delivery(
        "push",
        {
            "ref": "refs/heads/dev",
            "before": "b" * 40,
            "after": "c" * 40,
            "repository": {"full_name": "OmniNode-ai/omnibase_infra"},
        },
        "11111111-cc78-11e3-81ab-4c9367dc0958",
    )
    result = await handler.handle(delivery)
    (observation,) = result.events
    assert isinstance(observation, ModelGitHubBranchHeadObservation)
    assert observation.kind == "branch-ref-advanced"
    assert observation.entity_id == "OmniNode-ai/omnibase_infra@dev"
    assert observation.sha == "c" * 40
    assert observation.before_sha == "b" * 40

    unwatched = _signed_delivery(
        "push",
        {
            "ref": "refs/heads/jonah/some-feature",
            "before": "b" * 40,
            "after": "c" * 40,
            "repository": {"full_name": "OmniNode-ai/omnibase_infra"},
        },
        "22222222-cc78-11e3-81ab-4c9367dc0958",
    )
    assert (await handler.handle(unwatched)).events == ()


@pytest.mark.asyncio
async def test_wired_handler_separates_branch_head_from_merge_group_verdicts(
    handler: HandlerGitHubWebhookIngress,
) -> None:
    head = await handler.handle(
        _signed_delivery(
            "check_run",
            _summary_check_run("dev"),
            "33333333-cc78-11e3-81ab-4c9367dc0958",
        )
    )
    (head_observation,) = head.events
    assert isinstance(head_observation, ModelGitHubBranchHeadObservation)
    assert head_observation.kind == "branch-head-status"
    assert head_observation.ci_status == "SUCCESS"
    assert head_observation.check_name == "CI Summary"
    assert head_observation.merge_group_ref is None

    queue_ref = "gh-readonly-queue/dev/pr-4286-" + "e" * 40
    queued = await handler.handle(
        _signed_delivery(
            "check_run",
            _summary_check_run(queue_ref),
            "44444444-cc78-11e3-81ab-4c9367dc0958",
        )
    )
    (queue_observation,) = queued.events
    assert isinstance(queue_observation, ModelGitHubBranchHeadObservation)
    assert queue_observation.kind == "merge-group-status"
    assert queue_observation.branch == "dev"
    assert queue_observation.merge_group_ref == queue_ref
