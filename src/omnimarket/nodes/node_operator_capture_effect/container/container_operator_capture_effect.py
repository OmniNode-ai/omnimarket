# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnimarket.nodes.node_operator_capture_compute.handlers.handler_capture_rows import (
    HandlerCaptureRows,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_ruling_drift import (
    HandlerRulingDrift,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_utterance_classify import (
    HandlerUtteranceClassify,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_words_promotion import (
    HandlerWordsPromotion,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    DelegateRunner,
    LedgerAppender,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_digest import (
    HandlerCaptureDigest,
    PushSender,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_process import (
        HandlerCaptureProcess,
    )


def build_handler_utterance_classify() -> HandlerUtteranceClassify:
    """Build the utterance classify handler."""
    return HandlerUtteranceClassify()


def build_handler_ruling_drift() -> HandlerRulingDrift:
    """Build the ruling drift handler."""
    return HandlerRulingDrift()


def build_handler_capture_rows() -> HandlerCaptureRows:
    """Build the capture rows handler."""
    return HandlerCaptureRows()


def build_handler_words_promotion() -> HandlerWordsPromotion:
    """Build the words promotion handler."""
    return HandlerWordsPromotion()


def build_handler_capture_process(
    delegate: DelegateRunner,
    append: LedgerAppender,
) -> HandlerCaptureProcess:
    """Build the capture processor with its injected ports."""
    from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_process import (
        HandlerCaptureProcess,
    )

    return HandlerCaptureProcess(delegate, append)


def build_handler_capture_digest(*, push: PushSender) -> HandlerCaptureDigest:
    """Build the digest handler with its push sender."""
    return HandlerCaptureDigest(push=push)
