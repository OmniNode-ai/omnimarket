# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answers ``tenant-topic-provision-requested``: a tenant's wire topics, created and read back.

For each tenant the handler names every topic of the mirror set under the contract's tenant template, creates the ones
the broker does not list, and lists the broker again: a tenant is ``provisioned`` only when every name was read back.
A topic whose create is refused, or that the read-back does not show, is named in the answer and leaves the tenant
``unprovisioned``; the next run creates only what is still absent. The handler has no delete: it is given a lister
and a creator and nothing else to write with, so a backfill cannot remove or recreate a topic.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import yaml

from omnimarket.nodes.node_tenant_topic_provision_effect.adapters.forwarder_mirror_topics import (
    ForwarderContractMirrorTopics,
    MirrorTopicsUnavailableError,
)
from omnimarket.nodes.node_tenant_topic_provision_effect.models.model_tenant_topic_provision import (
    EnumTenantTopicOutcome,
    EnumTenantTopicState,
    ModelTenantTopicProvisionCommand,
    ModelTenantTopicProvisionResult,
    ModelTenantTopicReport,
)
from omnimarket.nodes.node_tenant_topic_provision_effect.protocols import (
    ProtocolMirrorTopicSource,
    ProtocolTenantTopicRegistry,
)
from omnimarket.projection.tenant_registry_resolution import is_tenant_slug


def utc_now() -> datetime:
    return datetime.now(UTC)


def _contract_settings() -> dict[str, Any]:
    text = (
        resources.files("omnimarket.nodes.node_tenant_topic_provision_effect")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    config: dict[str, Any] = yaml.safe_load(text)["config"]["tenant_topics"]
    return config


class HandlerTenantTopicProvision:
    """Creates the mirror set under each tenant's prefix on the injected broker.

    Every dependency is a constructor argument. Left out, the mirror source and the template come from this node's
    contract, the partition and replica counts and the broker lane from its overlay; the registry has no default.
    """

    def __init__(
        self,
        *,
        mirror_source: ProtocolMirrorTopicSource | None = None,
        registry: ProtocolTenantTopicRegistry | None = None,
        lister: Any = None,
        creator: Any = None,
        topic_template: str | None = None,
        num_partitions: int | None = None,
        replication_factor: int | None = None,
        broker_binding: AbstractContextManager[object] | None = None,
        clock: Any = utc_now,
    ) -> None:
        self._mirror_source = mirror_source
        self._registry = registry
        self._lister = lister
        self._creator = creator
        self._template = topic_template
        self._partitions = num_partitions
        self._replicas = replication_factor
        self._binding: AbstractContextManager[object] = broker_binding or nullcontext()
        self._clock = clock

    def _complete(self) -> None:
        """Fill what the caller left out from the contract and the overlay."""
        if self._mirror_source is None or self._template is None:
            settings = _contract_settings()
            if self._mirror_source is None:
                self._mirror_source = ForwarderContractMirrorTopics(
                    str(settings["forwarder_contract_module"])
                )
            if self._template is None:
                self._template = str(settings["topic_template"])
        if None in (self._lister, self._creator, self._partitions, self._replicas):
            from omnimarket.nodes.node_tenant_topic_provision_effect.overlay import (
                build_broker,
                load_overlay,
            )

            overlay = load_overlay()
            if self._lister is None or self._creator is None:
                self._lister, self._creator, self._binding = build_broker(overlay)
            if self._partitions is None:
                self._partitions = overlay.num_partitions
            if self._replicas is None:
                self._replicas = overlay.replication_factor

    def _answer(
        self,
        command: ModelTenantTopicProvisionCommand,
        outcome: EnumTenantTopicOutcome,
        detail: str,
        tenants: tuple[ModelTenantTopicReport, ...] = (),
    ) -> ModelTenantTopicProvisionResult:
        return ModelTenantTopicProvisionResult(
            command_id=command.command_id,
            outcome=outcome,
            detail=detail,
            tenants=tenants,
            emitted_at=self._clock(),
        )

    async def handle(
        self, request: ModelTenantTopicProvisionCommand
    ) -> ModelTenantTopicProvisionResult:
        if request.tenant_slug is not None and not is_tenant_slug(request.tenant_slug):
            return self._answer(
                request,
                EnumTenantTopicOutcome.REFUSED_INVALID_SLUG,
                "tenant_slug is not a valid tenant slug; nothing created",
            )
        self._complete()
        assert self._mirror_source is not None
        assert self._template is not None
        assert self._partitions is not None
        assert self._replicas is not None
        try:
            names = self._mirror_source.mirror_topic_names()
        except MirrorTopicsUnavailableError as exc:
            return self._answer(
                request, EnumTenantTopicOutcome.FAILED_NO_MIRROR_TOPICS, str(exc)
            )
        if not names:
            return self._answer(
                request,
                EnumTenantTopicOutcome.FAILED_NO_MIRROR_TOPICS,
                "the mirror set is empty; nothing created",
            )
        if self._registry is None:
            return self._answer(
                request,
                EnumTenantTopicOutcome.FAILED_TENANT_SOURCE_UNAVAILABLE,
                "no tenant registry was given; nothing created",
            )
        try:
            slugs = (
                (request.tenant_slug,)
                if request.tenant_slug is not None
                else tuple(await self._registry.list_tenant_slugs())
            )
        except Exception as exc:
            return self._answer(
                request,
                EnumTenantTopicOutcome.FAILED_TENANT_SOURCE_UNAVAILABLE,
                f"tenant list unavailable: {type(exc).__name__}",
            )
        wanted = {
            slug: tuple(self._template.format(slug=slug, topic=name) for name in names)
            for slug in slugs
        }
        with self._binding:
            reports, broker_error = await self._apply(wanted)
        if broker_error is not None:
            for slug in slugs:
                await self._record(slug, EnumTenantTopicState.UNPROVISIONED, ())
            return self._answer(
                request, EnumTenantTopicOutcome.FAILED_BROKER_UNAVAILABLE, broker_error
            )
        recorded = [
            await self._record(r.tenant_slug, r.state, r.failed_topics) for r in reports
        ]
        failed = [r for r in reports if r.state is EnumTenantTopicState.UNPROVISIONED]
        if not all(recorded):
            return self._answer(
                request,
                EnumTenantTopicOutcome.FAILED_TENANT_SOURCE_UNAVAILABLE,
                "the topic state could not be recorded for every tenant",
                tuple(reports),
            )
        if failed:
            named = "; ".join(
                f"{r.tenant_slug}: {', '.join(r.failed_topics) or r.detail}"
                for r in failed
            )
            return self._answer(
                request,
                EnumTenantTopicOutcome.FAILED_TOPICS,
                f"unprovisioned: {named}",
                tuple(reports),
            )
        created = sum(len(r.created) for r in reports)
        return self._answer(
            request,
            EnumTenantTopicOutcome.PROVISIONED
            if created
            else EnumTenantTopicOutcome.ALREADY_PROVISIONED,
            f"{len(reports)} tenant(s) provisioned; {created} topic(s) created",
            tuple(reports),
        )

    async def _record(
        self,
        slug: str,
        state: EnumTenantTopicState,
        failed_topics: tuple[str, ...],
    ) -> bool:
        assert self._registry is not None
        try:
            await self._registry.record_state(slug, state, failed_topics)
        except Exception:
            return False
        return True

    async def _apply(
        self, wanted: dict[str, tuple[str, ...]]
    ) -> tuple[list[ModelTenantTopicReport], str | None]:
        try:
            live = set(await self._lister.list_topics())
        except Exception as exc:
            return [], f"broker topic list failed: {type(exc).__name__}"
        created: dict[str, list[str]] = {slug: [] for slug in wanted}
        existing: dict[str, list[str]] = {slug: [] for slug in wanted}
        errors: dict[str, list[str]] = {slug: [] for slug in wanted}
        for slug, topics in wanted.items():
            for topic in topics:
                if topic in live:
                    existing[slug].append(topic)
                    continue
                try:
                    made = await self._creator.create_topic(
                        topic, self._partitions, self._replicas
                    )
                except Exception as exc:
                    # named by the read-back below, never reported as created
                    errors[slug].append(f"{topic}: {type(exc).__name__}")
                    continue
                (created if made else existing)[slug].append(topic)
        try:
            read_back = set(await self._lister.list_topics())
        except Exception as exc:
            return [], f"broker read-back failed: {type(exc).__name__}"
        reports: list[ModelTenantTopicReport] = []
        for slug, topics in wanted.items():
            seen = tuple(sorted(t for t in topics if t in read_back))
            missing = tuple(sorted(t for t in topics if t not in read_back))
            reports.append(
                ModelTenantTopicReport(
                    tenant_slug=slug,
                    state=EnumTenantTopicState.UNPROVISIONED
                    if missing
                    else EnumTenantTopicState.PROVISIONED,
                    created=tuple(created[slug]),
                    existing=tuple(existing[slug]),
                    read_back=seen,
                    failed_topics=missing,
                    create_errors=tuple(errors[slug]),
                    detail=(
                        f"{len(missing)} topic(s) absent from the broker"
                        if missing
                        else "every topic read back"
                    ),
                )
            )
        return reports, None


__all__ = ["HandlerTenantTopicProvision", "utc_now"]
