# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory backends of the J10 join: a receipt store and a runtime whose answer the caller sets (OMN-22957)."""

from __future__ import annotations

from omnimarket.models.model_dev_deploy_reading import (
    ModelLabPassReceiptFact,
)
from omnimarket.nodes.node_dev_deploy_reading_compute.models.model_join_j10 import (
    ModelReceiptRead,
    ModelRuntimeAnswer,
    ModelRuntimeAsk,
)


class InMemoryLabPassReceipts:
    def __init__(self, unread_reason: str | None = None) -> None:
        self._receipts: list[ModelLabPassReceiptFact] = []
        self._unread_reason = unread_reason

    def add(self, receipt: ModelLabPassReceiptFact) -> None:
        self._receipts.append(receipt)

    def read_receipts(self, sha: str) -> ModelReceiptRead:
        if self._unread_reason is not None:
            return ModelReceiptRead(receipts=(), unread_reason=self._unread_reason)
        return ModelReceiptRead(
            receipts=tuple(r for r in self._receipts if r.sha == sha),
            unread_reason=None,
        )


class InMemoryRuntime:
    """A runtime that reports ``sha`` and echoes the asked correlation id unless told otherwise."""

    def __init__(
        self,
        sha: str | None,
        *,
        reachable: bool = True,
        functional_ok: bool = True,
        echo: bool = True,
    ) -> None:
        self.sha = sha
        self.reachable = reachable
        self.functional_ok = functional_ok
        self.echo = echo
        self.asked: list[ModelRuntimeAsk] = []

    def ask(self, ask: ModelRuntimeAsk) -> ModelRuntimeAnswer:
        self.asked.append(ask)
        if not self.reachable:
            return ModelRuntimeAnswer(
                reachable=False,
                observed_sha=None,
                echoed_correlation_id=None,
                functional_ok=False,
                detail="in-memory runtime is down",
            )
        return ModelRuntimeAnswer(
            reachable=True,
            observed_sha=self.sha,
            echoed_correlation_id=ask.correlation_id if self.echo else None,
            functional_ok=self.functional_ok,
            detail="in-memory runtime answered",
        )


__all__: list[str] = ["InMemoryLabPassReceipts", "InMemoryRuntime"]
