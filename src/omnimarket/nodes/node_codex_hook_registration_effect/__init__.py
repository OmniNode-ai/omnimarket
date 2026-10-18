# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeCodexHookRegistrationEffect: merges the shipped Codex hooks (OMN-18759).

Capability: hook.codex.registration_apply. The merge preserves existing hooks
and backs up the user hooks file before applying missing commands.

The registration file is read at run time from the omniclaude plugin root passed in.
"""
