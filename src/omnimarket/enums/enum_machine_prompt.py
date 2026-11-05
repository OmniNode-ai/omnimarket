# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prompt prefixes shared by capture, arrival hooks and structural detection."""

# Text a machine put in the prompt slot. Matched on the stripped start of the message.
MACHINE_PREFIXES: tuple[str, ...] = (
    "<task-notification",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<system-reminder>",
    "<wake",
    "<webhook-payload",
    "<child-session-event",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<user-prompt-submit-hook>",
    "<scheduled-task",
    "[workflow harness",
    "[request interrupted",
    "caveat: the messages below were generated",
    "this session is being continued from a previous conversation",
)
