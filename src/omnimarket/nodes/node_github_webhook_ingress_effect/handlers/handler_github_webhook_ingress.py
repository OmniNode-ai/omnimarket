# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerGitHubWebhookIngress - verify a GitHub delivery, fold it, emit (OMN-19492).

Consumes ``onex.cmd.github.webhook-delivery.v1``. For each command it:

1. resolves the App webhook secret through the ``SecretResolver`` the kernel
   builds from the lane's rendered resolver config (logical name
   ``github.webhook.secret``), and refuses when there is none (fail closed: an
   unverifiable feed must never produce a PR-state observation);
2. re-verifies ``X-Hub-Signature-256`` over the exact received bytes, in
   constant time -- the door checked it too, but the command crossed the
   gateway forwarder and this node trusts nothing it did not verify;
3. folds the delivery (``webhook_fold.fold_delivery``, pure) into PR-state
   observations for ``onex.evt.github.pr-status.v1`` and, for a merge, one
   event for ``onex.evt.github.pr-merged.v1``, plus watched branch ref and CI
   observations for ``onex.evt.github.branch-head.v1``;
4. returns them for the runtime to publish on the contract's
   ``event_bus.publish_topics``; the handler holds no bus client.

A refused or malformed delivery raises a typed error, so the runtime routes it
to the dead-letter topic where it is visible; it is never acknowledged as a
success that wrote nothing. Logs carry the delivery id, the event name, the
byte length and the outcome -- never the secret, the signature or the body.

Idempotency needs no table: a redelivery reuses the delivery id and folds to
identical observations, a consumer folding identical values changes nothing,
and a merged event's id is a UUIDv5 of (repo, number, merge sha), which the
pr_merged_events projection dedupes on.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import UUID, uuid4

import yaml
from omnibase_core.enums import EnumCoreErrorCode
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_infra.enums import (
    EnumHandlerType,
    EnumHandlerTypeCategory,
    EnumInfraTransportType,
)
from omnibase_infra.errors import ModelInfraErrorContext, RuntimeHostError
from omnibase_infra.runtime.secret_resolver import SecretResolver

from omnimarket.nodes.node_github_webhook_ingress_effect.models import (
    ModelGitHubWebhookDelivery,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.webhook_fold import (
    WebhookFoldError,
    fold_delivery,
    verify_signature,
)

logger = logging.getLogger(__name__)

HANDLER_ID_GITHUB_WEBHOOK_INGRESS: str = "github-webhook-ingress-handler"
# The logical secret name the lane's resolver config maps (runtime policy
# contract, dev profile). A name, not a value.
WEBHOOK_SECRET_REF: str = "github.webhook.secret"
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_summary_check_names(contract_path: Path = _CONTRACT_PATH) -> frozenset[str]:
    """The contract's ``config.summary_check_names``: which check runs are a PR's verdict."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    config = raw.get("config") if isinstance(raw, dict) else None
    names = config.get("summary_check_names") if isinstance(config, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        raise ValueError(
            f"{contract_path}: config.summary_check_names must be a non-empty list of names"
        )
    return frozenset(names)


def load_watched_branches(contract_path: Path = _CONTRACT_PATH) -> frozenset[str]:
    """Load the contract's non-empty ``config.watched_branches`` list."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    config = raw.get("config") if isinstance(raw, dict) else None
    branches = config.get("watched_branches") if isinstance(config, dict) else None
    if (
        not isinstance(branches, list)
        or not branches
        or not all(isinstance(branch, str) and branch.strip() for branch in branches)
    ):
        raise ValueError(
            f"{contract_path}: config.watched_branches must be a non-empty list of strings"
        )
    return frozenset(branches)


class HandlerGitHubWebhookIngress:
    """EFFECT handler: signed delivery in, PR, merge and branch observations out."""

    def __init__(
        self,
        secret_resolver: SecretResolver | None = None,
        summary_check_names: frozenset[str] | None = None,
        *,
        webhook_secret: str | None = None,
        watched_branches: frozenset[str] | None = None,
    ) -> None:
        """Build the handler.

        Args:
            secret_resolver: Built by the kernel from the lane's rendered
                resolver config when that config maps ``github.webhook.secret``
                (service_kernel ``_github_webhook_ingress_dependencies``). With
                none, every delivery is refused.
            summary_check_names: Check-run names that stand for a PR's CI
                verdict; defaults to the contract's ``config.summary_check_names``.
            webhook_secret: A literal secret, for tests and replay tools only;
                it takes precedence over the resolver.
            watched_branches: Branches to observe; defaults to the contract's
                ``config.watched_branches``.
        """
        self._secret_resolver = secret_resolver
        self._literal_secret = webhook_secret
        self._summary_check_names = (
            summary_check_names
            if summary_check_names is not None
            else load_summary_check_names()
        )
        self._watched_branches = (
            watched_branches
            if watched_branches is not None
            else load_watched_branches()
        )

    async def _webhook_secret(self) -> bytes:
        if self._literal_secret is not None:
            return self._literal_secret.strip().encode("utf-8")
        if self._secret_resolver is None:
            return b""
        value = await self._secret_resolver.get_secret_async(
            WEBHOOK_SECRET_REF, required=False
        )
        return value.get_secret_value().strip().encode("utf-8") if value else b""

    @property
    def handler_type(self) -> EnumHandlerType:
        return EnumHandlerType.NODE_HANDLER

    @property
    def handler_category(self) -> EnumHandlerTypeCategory:
        return EnumHandlerTypeCategory.EFFECT

    async def handle(
        self,
        delivery: ModelGitHubWebhookDelivery,
    ) -> ModelHandlerOutput[None]:
        """Verify, fold and return the observations for the runtime to publish."""
        correlation_id = uuid4()
        secret = await self._webhook_secret()
        if not secret:
            self._refuse(delivery, correlation_id, "no webhook secret configured")
        try:
            body = base64.b64decode(delivery.body_b64, validate=True)
        except (binascii.Error, ValueError):
            self._refuse(delivery, correlation_id, "body is not valid base64")
        if not verify_signature(secret, body, delivery.signature_256):
            self._refuse(delivery, correlation_id, "signature does not verify")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._refuse(delivery, correlation_id, "body is not UTF-8 JSON")
        if not isinstance(payload, dict):
            self._refuse(delivery, correlation_id, "body is not a JSON object")
        try:
            events = fold_delivery(
                event=delivery.event,
                delivery_id=delivery.delivery_id,
                payload=payload,
                received_at=delivery.received_at,
                published_at=datetime.now(UTC),
                summary_check_names=self._summary_check_names,
                watched_branches=self._watched_branches,
            )
        except WebhookFoldError as exc:
            self._refuse(delivery, correlation_id, f"unexpected shape: {exc}")
        logger.info(
            "github webhook delivery folded",
            extra={
                "delivery_id": delivery.delivery_id,
                "github_event": delivery.event,
                "body_bytes": len(body),
                "observations": len(events),
                "outcome": "folded" if events else "ignored",
            },
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=correlation_id,
            handler_id=HANDLER_ID_GITHUB_WEBHOOK_INGRESS,
            events=events,
        )

    @staticmethod
    def _refuse(
        delivery: ModelGitHubWebhookDelivery,
        correlation_id: UUID,
        reason: str,
    ) -> NoReturn:
        logger.warning(
            "github webhook delivery refused",
            extra={
                "delivery_id": delivery.delivery_id,
                "github_event": delivery.event,
                "outcome": "refused",
                "reason": reason,
            },
        )
        context = ModelInfraErrorContext.with_correlation(
            correlation_id=correlation_id,
            transport_type=EnumInfraTransportType.KAFKA,
            operation="github_webhook.ingress",
        )
        raise RuntimeHostError(
            f"GitHub webhook delivery {delivery.delivery_id} refused: {reason}",
            error_code=EnumCoreErrorCode.INVALID_INPUT,
            context=context,
        )


__all__: list[str] = [
    "HANDLER_ID_GITHUB_WEBHOOK_INGRESS",
    "WEBHOOK_SECRET_REF",
    "HandlerGitHubWebhookIngress",
    "load_summary_check_names",
    "load_watched_branches",
]
