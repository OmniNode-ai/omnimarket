# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Call the capture digest's alert-channel library directly, outside attention gating."""

import subprocess
from pathlib import Path

from omnimarket.models.operator_answer import ModelAnswerSendReceipt


class AlertChannelAnswerSender:
    def __init__(
        self, library: Path, *, timeout_seconds: float, env_file: Path | None = None
    ) -> None:
        self._library = library
        self._timeout = timeout_seconds
        self._env_file = env_file

    def send(self, category: str, text: str) -> ModelAnswerSendReceipt:
        try:
            outcome = subprocess.run(
                [
                    "bash",
                    "-c",
                    'set -a; if [ -n "$1" ]; then . "$1" || exit; fi; set +a; . "$2" && alert_channel_send "$3" "$4"',
                    "answer-channel",
                    str(self._env_file) if self._env_file else "",
                    str(self._library),
                    category,
                    text,
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ModelAnswerSendReceipt(accepted=False, detail=type(exc).__name__)
        # alert_channel_send accepts with exit 0 and deliberately emits no send id.
        # Record that actual acceptance, never invent a provider message timestamp.
        accepted = outcome.returncode == 0
        return ModelAnswerSendReceipt(
            accepted=accepted,
            receipt="alert_channel_send:exit=0" if accepted else "",
            detail=""
            if accepted
            else f"UNREAD alert_channel_send:exit={outcome.returncode}",
        )
