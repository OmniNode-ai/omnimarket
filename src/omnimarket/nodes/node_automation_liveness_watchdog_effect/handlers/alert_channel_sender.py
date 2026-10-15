# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm sender over the alert-channel library's ``alert_channel_alarm``.

Anything short of a Slack message timestamp on stdout with exit 0 is a delivery
failure: a non-zero exit (credentials unresolved, dead channel), the exit 2 and
empty output of ``alert_channel_send`` without credentials, a zero exit that
printed nothing, a missing library, a timeout. Credentials are resolved by the
library itself; this module never reads or prints them.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from uuid import UUID

from omnimarket.nodes.node_automation_liveness_watchdog_effect.models.model_watchdog_delivery import (
    ModelAlarmSendReceipt,
)

_SLACK_TS = re.compile(r"^\d{9,}\.\d{3,}$")
_STDERR_TAIL = 200


def _failed(reason: str) -> ModelAlarmSendReceipt:
    return ModelAlarmSendReceipt(delivered=False, failure=reason)


class AlertChannelAlarmSender:
    def __init__(self, library: Path, *, timeout_seconds: int = 30) -> None:
        self._library = library
        self._timeout = timeout_seconds

    def send(
        self, *, episode_id: UUID, category: str, text: str
    ) -> ModelAlarmSendReceipt:
        if not self._library.is_file():
            return _failed(f"alert-channel library not found: {self._library}")
        try:
            proc = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1" && alert_channel_alarm "$2" "$3"',
                    "alert-channel",
                    str(self._library),
                    category,
                    text,
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _failed(f"alert_channel_alarm timed out after {self._timeout}s")
        except OSError as exc:
            return _failed(f"alert_channel_alarm could not start: {exc}")
        if proc.returncode != 0:
            tail = proc.stderr.strip()[-_STDERR_TAIL:]
            return _failed(
                f"alert_channel_alarm returned {proc.returncode}: {tail}".rstrip(": ")
            )
        lines = proc.stdout.strip().splitlines()
        stamp = lines[-1].strip() if lines else ""
        if not _SLACK_TS.match(stamp):
            return _failed(
                "alert_channel_alarm returned 0 but printed no Slack message timestamp"
            )
        return ModelAlarmSendReceipt(delivered=True, message_ref=stamp)


__all__ = ["AlertChannelAlarmSender"]
