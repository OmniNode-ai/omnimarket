# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports for the daily evidence effect; deployment supplies the concrete nodes."""

from datetime import datetime
from typing import Protocol

from pydantic import JsonValue

from omnimarket.models.error_rule_measurement import (
    ModelCommittedMeasurement,
    ModelDailyMeasurementSnapshot,
    ModelDemotionProposal,
    ModelLabellingRequest,
    ModelLabelSet,
    ModelMeasurementProposal,
    ModelReplayRecord,
)


class ProtocolMeasurementArtifacts(Protocol):
    """Immutable content storage; get addresses exact bytes, not a mutable alias."""

    def get(self, digest: str) -> bytes: ...
    def put(self, content: bytes) -> str: ...


class ProtocolMeasurementReplay(Protocol):
    """Call the public normalizer/matcher using only these frozen input facts."""

    def __call__(
        self,
        row: dict[str, JsonValue],
        records: tuple[ModelReplayRecord, ...],
        normalizer: dict[str, JsonValue],
    ) -> tuple[str, ...]: ...


class ProtocolMeasurementLabeller(Protocol):
    """Second-engine run labels every sampled match and prospective capture."""

    def __call__(self, request: ModelLabellingRequest) -> ModelLabelSet: ...


class ProtocolMeasurementWriter(Protocol):
    """SCM boundary, implemented with the PR write node, under its declared login.

    write commits the measured block and returns its immutable commit id. read
    obtains the committed bytes and commit login independently from SCM, rather
    than echoing the request. demote opens/updates a shadow-mode PR with an
    expected snapshot guard; it never mutates a live row directly. Both write
    operations must persist their idempotency keys across crashes and restarts.
    """

    def write(self, proposal: ModelMeasurementProposal) -> str: ...
    def read(self, key: str) -> ModelCommittedMeasurement | None: ...
    def demote(self, proposal: ModelDemotionProposal) -> str: ...


class ProtocolMeasurementSource(Protocol):
    """Freeze the whole current table, corpus, normalizer and live ledger together.

    The source reads lab artifacts through their declared providers. It must not
    filter out shadow rows or rows with no matches; they need honest zero evidence.
    """

    def snapshot(self, now: datetime) -> ModelDailyMeasurementSnapshot: ...
