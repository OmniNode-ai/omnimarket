# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind a validated policy to deployment-owned bus, lease and terminal ports."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from omnimarket.models.operator_answer import ModelAnswerContext
from omnimarket.nodes.node_operator_answer_effect.handlers.answer_channel import (
    AlertChannelAnswerSender,
)
from omnimarket.nodes.node_operator_answer_effect.handlers.answer_overlay import (
    load_answer_config,
)
from omnimarket.nodes.node_operator_answer_effect.handlers.answer_reply import (
    CheckedAnswerWriter,
)
from omnimarket.nodes.node_operator_answer_effect.handlers.answer_store import (
    SqliteAnswerStore,
)
from omnimarket.nodes.node_operator_answer_effect.node import NodeOperatorAnswerEffect
from omnimarket.nodes.node_operator_answer_effect.protocols.protocol_answer import (
    ProtocolAnswerLanes,
    ProtocolAnswerLedger,
    ProtocolAnswerPrinter,
    ProtocolAnswerSource,
)


def build_answer_node(
    *,
    overlay: Path,
    store_path: Path | None,
    alert_library: Path,
    source: ProtocolAnswerSource,
    lanes: ProtocolAnswerLanes,
    writer: Callable[[ModelAnswerContext], str],
    ledger: ProtocolAnswerLedger,
    printer: ProtocolAnswerPrinter,
    clock: Callable[[], datetime],
    alert_env_file: Path | None = None,
) -> NodeOperatorAnswerEffect:
    config = load_answer_config(overlay)
    return NodeOperatorAnswerEffect(
        config=config,
        store=SqliteAnswerStore(store_path),
        source=source,
        lanes=lanes,
        reply=CheckedAnswerWriter(writer),
        ledger=ledger,
        printer=printer,
        clock=clock,
        channel=AlertChannelAnswerSender(
            alert_library,
            timeout_seconds=config.alert_timeout_seconds,
            env_file=alert_env_file,
        ),
    )
