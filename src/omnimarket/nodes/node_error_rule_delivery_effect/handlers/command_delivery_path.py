# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Send a synthetic failure through the host's actual installed delivery entrypoint."""

import json
import subprocess

from omnimarket.models.error_rule_delivery import (
    ModelDeliveryFailure,
    ModelDeliveryPathCommand,
)


class CommandDeliveryPath:
    def __init__(self, binding: ModelDeliveryPathCommand) -> None:
        self.binding = binding

    def deliver(self, failure: ModelDeliveryFailure) -> None:
        # Same raw fields as a harness failure. The explicit probe marker must
        # survive the edge adapter, so the matcher's receipt proves this path
        # ran; a command's zero exit alone is never evidence of delivery.
        payload = {
            "hook_event_name": failure.hook_event_name,
            "session_id": failure.session_id,
            "tool_use_id": failure.tool_use_id,
            "tool_name": failure.tool_name,
            "error": failure.error_text,
            "onex_delivery_probe": {"id": failure.probe_id, "surface": failure.surface},
        }
        completed = subprocess.run(
            self.binding.command,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            timeout=self.binding.timeout_seconds,
        )
        if completed.returncode:
            # Diagnostics may contain credentials from an external hook. Keep
            # them on that hook's own log; the signal names only the exit code.
            raise OSError(
                f"installed delivery entrypoint exited {completed.returncode}"
            )
