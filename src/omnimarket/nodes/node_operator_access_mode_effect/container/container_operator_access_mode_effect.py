# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from ..handlers.handler_access_mode_detect import HandlerAccessModeDetect
from ..handlers.handler_attention_push_gate import HandlerAttentionPushGate
from ..handlers.handler_foreground_bash_guard import HandlerForegroundBashGuard
from ..handlers.handler_lab_dispatch_gate import HandlerLabDispatchGate
from ..handlers.handler_operator_reply_check import HandlerOperatorReplyCheck
from ..handlers.handler_remote_setup_check import HandlerRemoteSetupCheck
from ..handlers.handler_silence_guard import HandlerSilenceGuard


def build_handler_access_mode_detect() -> HandlerAccessModeDetect:
    """Build the access mode detect handler."""
    return HandlerAccessModeDetect()


def build_handler_remote_setup_check() -> HandlerRemoteSetupCheck:
    """Build the remote setup check handler."""
    return HandlerRemoteSetupCheck()


def build_handler_lab_dispatch_gate() -> HandlerLabDispatchGate:
    """Build the lab dispatch gate handler."""
    return HandlerLabDispatchGate()


def build_handler_foreground_bash_guard() -> HandlerForegroundBashGuard:
    """Build the foreground bash guard handler."""
    return HandlerForegroundBashGuard()


def build_handler_attention_push_gate() -> HandlerAttentionPushGate:
    """Build the attention push gate handler."""
    return HandlerAttentionPushGate()


def build_handler_operator_reply_check() -> HandlerOperatorReplyCheck:
    """Build the operator reply check handler."""
    return HandlerOperatorReplyCheck()


def build_handler_silence_guard() -> HandlerSilenceGuard:
    """Build the silence guard handler."""
    return HandlerSilenceGuard()
