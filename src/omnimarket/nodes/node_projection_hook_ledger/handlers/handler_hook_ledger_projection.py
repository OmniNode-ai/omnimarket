# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Cloud hook-event ledger projection runner (OMN-17201, leg 5).

Shape A (OMN-15905): a standalone ``BaseProjectionRunner`` Kafka consumer with
its own dedicated Deployment, NOT a contract loaded into the shared runtime
kernel. That is not a style choice -- this node's physical subscribe topics are
tenant-prefixed CLOUD WIRE topics, and the shared kernel dispatches on bare
canonical topics.

NAMING. The class is ``HandlerHookLedgerProjection``, not
``...ProjectionRunner``, even though every sibling standalone writer in this
repo carries the ``Runner`` suffix. ``Runner`` is a hardfail type-word in the
OMN-14350 non-canonical lifecycle-class ratchet; the siblings predate it and sit
in its frozen allowlist, which may only SHRINK. Adding brand-new code to that
allowlist would be using an allowlist as a fix.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml
from omnibase_infra.nodes.node_bus_forwarder_effect.services.service_gateway_topic_transform import (
    resolve_physical_topic,
)

from omnimarket.nodes.node_projection_hook_ledger.models.model_hook_ledger_event import (
    HookLedgerProjectionError,
    derive_hook_ledger_row,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

logger = logging.getLogger(__name__)

#: BARE table name, deliberately. The projection adapter's identifier validator
#: rejects a dotted name, so schema qualification is the runtime's job, resolved
#: from this contract's ``db_io.db_tables[0].schema``. Same split
#: ``node_projection_work_events`` uses.
TABLE = "hook_events"
SCHEMA = "public"

#: The table's own UNIQUE constraint. This string is asserted by the test suite
#: against the contract's ``idempotency.hash_fields`` so the SQL and the
#: contract cannot drift apart silently.
CONFLICT_KEY = "(tenant_id, event_sha)"

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"

#: Published only when a row actually landed -- see the contract.
APPLIED_TOPIC_KEY = "publish_topics"

_UPSERT_SQL = f"""
INSERT INTO {SCHEMA}.{TABLE} (
    tenant_id, event_sha, event_type, occurred_at, payload,
    event_id, correlation_id, run_id, source, batch_sha
)
VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10)
ON CONFLICT (tenant_id, event_sha) DO NOTHING
RETURNING tenant_id, event_sha, event_type, occurred_at, correlation_id
"""


class HandlerHookLedgerProjection(BaseProjectionRunner):
    """Projects cloud-bus omniclaude hook events into ``public.hook_events``."""

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        self._load_contract(contract_path)

    # -- contract resolution -------------------------------------------------

    def _load_contract(self, contract_path: Path | None = None) -> None:
        """Resolve everything this runner needs from its own contract.

        Split out of ``__init__`` so the contract-derived half can be exercised
        without constructing a database adapter or a Kafka client.
        """
        with open(contract_path or _CONTRACT_PATH) as fh:
            contract: dict[str, Any] = yaml.safe_load(fh)
        self._contract = contract

        self._canonical_topics: tuple[str, ...] = tuple(
            contract["event_bus"]["subscribe_topics"]
        )
        scope = contract["config"]["hook_ledger"]["cloud_wire_scope"]
        self._tenant_slugs: tuple[str, ...] = tuple(scope["tenant_slugs"])
        self._wire_topics: tuple[str, ...] = tuple(
            self.resolve_wire_topics(self._tenant_slugs, self._canonical_topics)
        )
        self._applied_topics: tuple[str, ...] = tuple(
            contract["event_bus"]["publish_topics"]
        )
        self._dlq_topics: tuple[str, ...] = tuple(contract["event_bus"]["dlq_topics"])

    @staticmethod
    def resolve_wire_topics(
        tenant_slugs: Sequence[str] | Iterable[str],
        canonical_topics: Sequence[str] | Iterable[str],
    ) -> list[str]:
        """Resolve contract-declared canonical topics to physical cloud wire topics.

        Routed through ``resolve_physical_topic`` -- THE single runtime topic
        resolver (OMN-15792) -- rather than formatting the ``tenant-<slug>.``
        prefix here. A second implementation of that transform is the
        structural root cause OMN-15792 was filed to remove.

        Fail-closed on an empty tenant scope: resolving to zero subscriptions
        would produce a consumer that reports Stable with LAG 0 forever and is
        indistinguishable from a working writer.
        """
        slugs = list(tenant_slugs)
        if not slugs:
            raise ValueError(
                "hook ledger cloud_wire_scope.tenant_slugs is empty: refusing "
                "to start a writer that would subscribe to no topics at all."
            )
        topics = list(canonical_topics)
        if not topics:
            raise ValueError(
                "hook ledger contract declares no subscribe_topics: refusing "
                "to start a writer with nothing to consume."
            )
        return [
            resolve_physical_topic(topic, tenant_slug=slug)
            for slug in slugs
            for topic in topics
        ]

    # -- BaseProjectionRunner surface ----------------------------------------

    @property
    def topics(self) -> list[str]:
        return list(self._wire_topics)

    @property
    def poison_dlq_topics(self) -> list[str]:
        """OMN-13634: the base-class safety net routes escaped POISON here."""
        return list(self._dlq_topics)

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        """Supply the runtime-owned publisher to the base-class DLQ path."""
        publish = await self.get_publish_fn()
        if publish is None:
            logger.error(
                "node_projection_hook_ledger: no publisher for POISON DLQ "
                "topic %s -- the record cannot be quarantined",
                topic,
            )
            return
        await publish(topic, value)

    async def _publish_applied(self, row: dict[str, Any]) -> None:
        """Assert that a durable row landed. Never called for a suppressed duplicate."""
        publish = await self.get_publish_fn()
        if publish is None:
            return
        for topic in self._applied_topics:
            await publish(
                topic,
                json.dumps(
                    {
                        "tenant_id": row["tenant_id"],
                        "event_sha": row["event_sha"],
                        "event_type": row["event_type"],
                        "correlation_id": row["correlation_id"],
                        "occurred_at": row["occurred_at"],
                        "rows_upserted": 1,
                    },
                    default=str,
                ).encode("utf-8"),
            )

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Project one cloud-bus hook record into the ledger.

        Returns ``False`` for a topic outside the declared wire set. That is a
        genuine non-event, not a failure: the base runner commits the offset so
        an unrelated record on a shared subscription never wedges the leg. A
        record that IS in scope but cannot be derived raises
        ``HookLedgerProjectionError``, which classifies as POISON and is routed
        to the DLQ with the offset committed.
        """
        if topic not in self._wire_topics:
            logger.warning(
                "hook ledger received a record on %s, which is not in its "
                "declared wire topic set -- not projected",
                topic,
            )
            return False

        row = derive_hook_ledger_row(
            wire_topic=topic,
            data=data,
            partition=meta.partition,
            offset=meta.offset,
        )

        # tenant= is LOAD-BEARING, not decoration. public.hook_events carries
        # ENABLE + FORCE ROW LEVEL SECURITY (the owning node's migration 0002)
        # with a WITH CHECK predicate of
        # `tenant_id = current_setting('app.tenant_id', true)`. FORCE means the
        # table owner is not exempt either, so a write on a connection that
        # never set the GUC is refused -- every row, silently, forever. The
        # adapter sets it with the parameterized set_config form inside the
        # same transaction as the statement; SET LOCAL semantics only hold
        # inside one. That migration's own header quotes the defect this
        # avoids (OMN-15301: "the projection writer never sets app.tenant_id"),
        # and OMN-15306 is the same no-op with autocommit dropping the GUC
        # before the statement ran.
        #
        # It is passed NOW rather than when the migration is un-fenced,
        # because the alternative is a writer that works until the day someone
        # lifts the fence and then refuses every insert.
        written = await self.db.execute(
            _UPSERT_SQL,
            row["tenant_id"],
            row["event_sha"],
            row["event_type"],
            row["occurred_at"],
            json.dumps(row["payload"], default=str),
            row["event_id"],
            row["correlation_id"],
            row["run_id"],
            row["source"],
            row["batch_sha"],
            tenant=row["tenant_id"],
        )
        # ON CONFLICT DO NOTHING returns zero rows for a redelivery, so an
        # empty result is a SUPPRESSED DUPLICATE, not a failure -- the offset
        # is still committed and no applied-event is asserted.
        if written:
            await self._publish_applied(row)
        return True

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler-protocol shim; delegates to ``project_event``."""
        topic = str(input_data.pop("_topic", ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        projected = asyncio.run(self.project_event(topic, input_data, meta))
        return {"projected": projected}


__all__ = ["HandlerHookLedgerProjection", "HookLedgerProjectionError"]


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    asyncio.run(HandlerHookLedgerProjection().run())
