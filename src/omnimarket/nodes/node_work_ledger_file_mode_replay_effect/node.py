# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the file-mode replay queue."""

from pathlib import Path

from .handlers.handler_file_mode_replay import HandlerFileModeReplay


class NodeWorkLedgerFileModeReplayEffect(HandlerFileModeReplay):
    """Declarative entry point for the file-mode replay queue."""

    contract_path = Path(__file__).with_name("contract.yaml")
