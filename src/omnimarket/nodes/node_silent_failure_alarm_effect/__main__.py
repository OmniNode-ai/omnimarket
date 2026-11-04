# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installed host tick uses the alarm contract's command and terminal topics."""

from .handlers.handler_silent_failure_alarm_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
