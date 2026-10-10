# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerGitHubWebhookIngress: fail closed, verify, fold (OMN-19492)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from omnibase_infra.errors import RuntimeHostError

from omnimarket.models.model_github_pr_state_observation import (
    ModelGitHubPrStateObservation,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.handlers import (
    HandlerGitHubWebhookIngress,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.handlers.handler_github_webhook_ingress import (
    load_summary_check_names,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.models import (
    ModelGitHubPrMergedObservation,
    ModelGitHubWebhookDelivery,
)

pytestmark = pytest.mark.unit

SECRET = "test-webhook-secret"


def _delivery(
    payload: dict[str, object] | bytes,
    *,
    event: str = "pull_request",
    secret: str = SECRET,
    signature: str | None = None,
) -> ModelGitHubWebhookDelivery:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    sig = signature or (
        "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    )
    return ModelGitHubWebhookDelivery(
        event=event,
        delivery_id="72d3162e-cc78-11e3-81ab-4c9367dc0958",
        signature_256=sig,
        body_b64=base64.b64encode(body).decode(),
        received_at=datetime(2026, 9, 27, 23, 0, tzinfo=UTC),
    )


def _merged_payload() -> dict[str, object]:
    return {
        "action": "closed",
        "repository": {"full_name": "OmniNode-ai/omniclaude"},
        "pull_request": {
            "number": 7,
            "state": "closed",
            "merged": True,
            "draft": False,
            "title": "fix(OMN-1): x",
            "closed_at": "2026-09-27T22:59:30Z",
            "merged_at": "2026-09-27T22:59:30Z",
            "merge_commit_sha": "c" * 40,
            "head": {"ref": "jonah/omn-1-x", "sha": "a" * 40},
            "base": {"ref": "dev"},
        },
    }


@pytest.mark.asyncio
async def test_a_verified_merge_yields_state_and_merge_events() -> None:
    handler = HandlerGitHubWebhookIngress(webhook_secret=SECRET)
    out = await handler.handle(_delivery(_merged_payload()))
    kinds = [type(e) for e in out.events]
    assert kinds == [ModelGitHubPrStateObservation, ModelGitHubPrMergedObservation]


@pytest.mark.asyncio
async def test_no_secret_refuses_every_delivery() -> None:
    handler = HandlerGitHubWebhookIngress(webhook_secret="")
    with pytest.raises(RuntimeHostError, match="no webhook secret configured"):
        await handler.handle(_delivery(_merged_payload()))


@pytest.mark.asyncio
async def test_a_forged_signature_is_refused() -> None:
    handler = HandlerGitHubWebhookIngress(webhook_secret=SECRET)
    with pytest.raises(RuntimeHostError, match="signature does not verify"):
        await handler.handle(_delivery(_merged_payload(), secret="not-the-secret"))


@pytest.mark.asyncio
async def test_bad_base64_is_refused() -> None:
    handler = HandlerGitHubWebhookIngress(webhook_secret=SECRET)
    delivery = _delivery(_merged_payload()).model_copy(update={"body_b64": "@@@"})
    with pytest.raises(RuntimeHostError, match="base64"):
        await handler.handle(delivery)


@pytest.mark.asyncio
async def test_a_signed_non_json_body_is_refused() -> None:
    handler = HandlerGitHubWebhookIngress(webhook_secret=SECRET)
    with pytest.raises(RuntimeHostError, match="UTF-8 JSON"):
        await handler.handle(_delivery(b"not json"))


@pytest.mark.asyncio
async def test_a_signed_malformed_payload_is_refused() -> None:
    handler = HandlerGitHubWebhookIngress(webhook_secret=SECRET)
    with pytest.raises(RuntimeHostError, match="unexpected shape"):
        await handler.handle(_delivery({"action": "opened", "pull_request": {}}))


@pytest.mark.asyncio
async def test_an_ignored_event_publishes_nothing() -> None:
    handler = HandlerGitHubWebhookIngress(webhook_secret=SECRET)
    out = await handler.handle(_delivery({"zen": "hi"}, event="ping"))
    assert out.events == ()


@pytest.mark.asyncio
async def test_the_secret_comes_from_the_lane_secret_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime path: the kernel hands the handler a SecretResolver built
    from the lane's rendered config, which maps github.webhook.secret."""
    from omnibase_infra.runtime.models.model_secret_resolver_config import (
        ModelSecretResolverConfig,
    )
    from omnibase_infra.runtime.secret_resolver import SecretResolver

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    resolver = SecretResolver(
        config=ModelSecretResolverConfig.model_validate(
            {
                "enable_convention_fallback": False,
                "mappings": [
                    {
                        "logical_name": "github.webhook.secret",
                        "source": {
                            "source_type": "env",
                            "source_path": "GITHUB_WEBHOOK_SECRET",
                        },
                    }
                ],
            }
        )
    )
    handler = HandlerGitHubWebhookIngress(secret_resolver=resolver)
    out = await handler.handle(_delivery(_merged_payload()))
    assert len(out.events) == 2


@pytest.mark.asyncio
async def test_no_resolver_refuses_every_delivery() -> None:
    """A lane that maps no secret builds the handler with no resolver."""
    with pytest.raises(RuntimeHostError, match="no webhook secret configured"):
        await HandlerGitHubWebhookIngress().handle(_delivery(_merged_payload()))


def test_the_mirrored_tenant_stamp_validates() -> None:
    delivery = _delivery(_merged_payload())
    stamped = ModelGitHubWebhookDelivery.model_validate(
        {**delivery.model_dump(mode="json"), "tenant_id": "beta-gateway-canary"}
    )
    assert stamped.tenant_id == "beta-gateway-canary"


def test_contract_names_the_summary_checks() -> None:
    assert "CI Summary" in load_summary_check_names()
