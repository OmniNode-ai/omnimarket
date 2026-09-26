# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The 33 Claude Code hook event types the capture contract covers (OMN-19513)."""

from __future__ import annotations

from enum import StrEnum


class EnumClaudeHookEventName(StrEnum):
    """Every hook type Claude Code 2.1.283 fires, by its wire name.

    Mirrors ``EnumClaudeHookEventName`` in the omniclaude capture contract
    (``contract_hook_claude_capture.yaml``, one coverage row per member). The
    projection keeps its own copy because omnimarket does not depend on
    omniclaude; the fixture test pins the two lists together, so a hook type
    added on the producer side fails here instead of landing as an event the
    writer refuses at runtime.
    """

    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    POST_TOOL_BATCH = "PostToolBatch"
    NOTIFICATION = "Notification"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    USER_PROMPT_EXPANSION = "UserPromptExpansion"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    STOP = "Stop"
    STOP_FAILURE = "StopFailure"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    PRE_MODEL_SWITCH = "PreModelSwitch"
    POST_MODEL_SWITCH = "PostModelSwitch"
    PERMISSION_REQUEST = "PermissionRequest"
    PERMISSION_DENIED = "PermissionDenied"
    SETUP = "Setup"
    TEAMMATE_IDLE = "TeammateIdle"
    TASK_CREATED = "TaskCreated"
    TASK_COMPLETED = "TaskCompleted"
    ELICITATION = "Elicitation"
    ELICITATION_RESULT = "ElicitationResult"
    CONFIG_CHANGE = "ConfigChange"
    WORKTREE_CREATE = "WorktreeCreate"
    WORKTREE_REMOVE = "WorktreeRemove"
    INSTRUCTIONS_LOADED = "InstructionsLoaded"
    CWD_CHANGED = "CwdChanged"
    FILE_CHANGED = "FileChanged"
    DIRECTORY_ADDED = "DirectoryAdded"
    MESSAGE_DISPLAY = "MessageDisplay"


#: The hook types that close one tool call. ``tool_call_count`` on an agent
#: span counts exactly these, so a failed call is a call, not a gap.
TOOL_CALL_COMPLETIONS: frozenset[EnumClaudeHookEventName] = frozenset(
    {
        EnumClaudeHookEventName.POST_TOOL_USE,
        EnumClaudeHookEventName.POST_TOOL_USE_FAILURE,
    }
)
