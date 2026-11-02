# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports a runtime gives the node when it constructs it without any.

``LocalStepFiles`` reads and appends real files on the machine the node runs on. The
review reader and the verifier have no bound production adapter yet (the source-control
node exposes no list-reviews read, and the verifier node is a separate task), so their
default raises: the handler turns that into ``refuse``, which leaves the job red.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_agent_review_gate_accept_effect.models.model_gate_accept import (
    ModelReceiptVerifyOutcome,
    ModelReceiptVerifyRequest,
    ModelReviewView,
)


class PortNotBoundError(RuntimeError):
    """A port with no production adapter was used; the decision fails closed."""


class LocalStepFiles:
    def read_text(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def append_text(self, path: str, text: str) -> None:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(text)


class UnboundReviewReader:
    def reviews(self, repository: str, pull_request: int) -> list[ModelReviewView]:
        raise PortNotBoundError("no source-control review reader is bound")


class UnboundReceiptVerifier:
    def verify(self, request: ModelReceiptVerifyRequest) -> ModelReceiptVerifyOutcome:
        raise PortNotBoundError("no receipt verifier is bound")
