# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports of the nightly sender: the run reader, the command sender, the policy values and the sent record."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender import (
    ModelCollectAnswer,
    ModelCollectCommand,
    ModelNightlyRunView,
    ModelPublishAnswer,
    ModelPublishCommand,
    ModelRecordSettings,
    ModelSlotRecord,
)


class NightlyRunReadError(Exception):
    """The nightly's runs could not be read through the repository gateway."""


class NightlyCommandError(Exception):
    """A command was not sent or not answered: a bus that would not open, no terminal event, an answer of an unknown shape."""


class SenderStateError(Exception):
    """The sent record cannot be read or written, so no command is sent: a resend loop is worse than a failed run."""


class ProtocolNightlyRunReader(Protocol):
    """Lists a workflow's scheduled runs through the repository gateway node, newest first."""

    def list_runs(
        self, *, repository: str, workflow_file: str
    ) -> tuple[ModelNightlyRunView, ...]: ...


class ProtocolNightlyCommandSender(Protocol):
    """Sends one command to the collect effect and returns the answer its terminal event carries."""

    def collect_nightly(self, command: ModelCollectCommand) -> ModelCollectAnswer: ...

    def publish_attribution_record(
        self, command: ModelPublishCommand
    ) -> ModelPublishAnswer: ...


class ProtocolRecordSettingsReader(Protocol):
    """Reads the selection policy values the publish command carries."""

    def read(self) -> ModelRecordSettings: ...


class ProtocolSenderStateStore(Protocol):
    """The record of the nights the sender has settled, kept across a runtime restart."""

    def ensure_writable(self) -> None:
        """Raises :class:`SenderStateError` unless the record can be read and written."""
        ...

    def settled(self, slot_date: date) -> ModelSlotRecord | None: ...

    def record(self, slot: ModelSlotRecord) -> None: ...


__all__: list[str] = [
    "NightlyCommandError",
    "NightlyRunReadError",
    "ProtocolNightlyCommandSender",
    "ProtocolNightlyRunReader",
    "ProtocolRecordSettingsReader",
    "ProtocolSenderStateStore",
    "SenderStateError",
]
