# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipt sources of the release sibling-premise read effect."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.models.dev_merge_redeploy import (
    ModelDevMergeRedeployReceipt,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.lane_keywords import (
    lane_keywords,
)
from omnimarket.nodes.node_release_sibling_premise_effect.models.model_sibling_premise_read_failed import (
    EnumSiblingPremiseReadFailure,
    SiblingPremiseReadError,
)
from omnimarket.nodes.node_topic_archive_effect.handlers._kafka_topic_reader import (
    AiokafkaTopicReader,
)


def decode_receipt(value: bytes) -> ModelDevMergeRedeployReceipt | None:
    """The receipt in a JSON envelope, or None when the bytes are not one."""
    try:
        raw = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    payload = raw.get("payload", raw)
    if not isinstance(payload, dict):
        return None
    try:
        return ModelDevMergeRedeployReceipt.model_validate(payload)
    except ValidationError:
        return None


class KafkaReceiptSource:
    """Receipts read group-less from the broker through AiokafkaTopicReader (commits nothing)."""

    def __init__(self, reader: AiokafkaTopicReader) -> None:
        self._reader = reader
        self._skipped = 0

    def skipped(self) -> int:
        return self._skipped

    async def read(
        self, topic: str, since: datetime
    ) -> tuple[ModelDevMergeRedeployReceipt, ...]:
        receipts: list[ModelDevMergeRedeployReceipt] = []
        self._skipped = 0
        since_ms = int(since.timestamp() * 1000)
        try:
            partitions = await self._reader.partitions(topic)
            if not partitions:
                raise SiblingPremiseReadError(
                    EnumSiblingPremiseReadFailure.RECEIPTS_UNREADABLE,
                    f"the broker lists no partitions for {topic}",
                )
            for partition in partitions:
                low, high = await self._reader.watermarks(topic, partition)
                async for record in self._reader.read_range(
                    topic, partition, low, high
                ):
                    if record.timestamp_ms < since_ms:
                        continue
                    receipt = decode_receipt(_record_bytes(record.value_b64))
                    if receipt is None:
                        self._skipped += 1
                    else:
                        receipts.append(receipt)
        except SiblingPremiseReadError:
            raise
        except Exception as exc:
            raise SiblingPremiseReadError(
                EnumSiblingPremiseReadFailure.RECEIPTS_UNREADABLE,
                f"{type(exc).__name__}: {exc}"[:1500],
            ) from exc
        return tuple(receipts)


def _record_bytes(value_b64: str | None) -> bytes:
    """The record's value bytes; a tombstone or a bad encoding is bytes that decode to no receipt."""
    if value_b64 is None:
        return b""
    try:
        return base64.b64decode(value_b64, validate=True)
    except (binascii.Error, ValueError):
        return b""


class InMemoryReceiptSource:
    """Receipts decoded from raw envelope bytes (the in-memory default profile)."""

    def __init__(self, envelopes: list[bytes]) -> None:
        self._envelopes = envelopes
        self._skipped = 0

    def skipped(self) -> int:
        return self._skipped

    async def read(
        self, topic: str, since: datetime
    ) -> tuple[ModelDevMergeRedeployReceipt, ...]:
        receipts: list[ModelDevMergeRedeployReceipt] = []
        self._skipped = 0
        for value in self._envelopes:
            receipt = decode_receipt(value)
            if receipt is None:
                self._skipped += 1
            else:
                receipts.append(receipt)
        return tuple(receipts)


@asynccontextmanager
async def open_receipt_source(
    *,
    bus: str,
    lane: str | None,
    kafka_bootstrap: str | None,
    workspace_root: Path | None,
) -> AsyncIterator[InMemoryReceiptSource | KafkaReceiptSource]:
    """Yield the receipt source for the profile; close the reader on exit.

    The kafka profile resolves the broker address of the lane the way
    ``omnimarket.delegated_test_loop.lane_bus.open_lab_run_bus`` does. It binds NO client
    transport itself: one process dispatches to one lane, so the caller opens the bus (which
    binds the lane's transport, and whose environment the reader's broker credentials are built
    from) first and opens this source inside it. Differs from the bus opener in what it yields:
    a group-less topic reader over the whole retained topic instead of a subscribing bus.
    """
    if bus == "inmemory":
        if lane is not None or kafka_bootstrap is not None:
            raise LabRunBusError(
                "--bus-lane and --kafka-bootstrap name a broker; the in-memory bus has none"
            )
        yield InMemoryReceiptSource([])
        return

    from omnibase_infra.cli.delegate_lane import (
        DelegateLaneSelectionError,
        resolve_lane_target,
    )

    try:
        target = resolve_lane_target(
            **lane_keywords(
                resolve_lane_target, "kafka", lane, kafka_bootstrap, workspace_root
            )
        )
    except DelegateLaneSelectionError as exc:
        raise LabRunBusError(str(exc)) from exc
    bootstrap = target.bootstrap_servers if target is not None else kafka_bootstrap
    if not bootstrap:
        raise LabRunBusError("no broker address was resolved")
    reader = AiokafkaTopicReader(bootstrap)
    try:
        yield KafkaReceiptSource(reader)
    finally:
        await reader.close()
