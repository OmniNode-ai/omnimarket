# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 OmniNode Team
"""Invocation-only ADK adapter package (OMN-13611, WS-C Phase 2.1).

Exposes the canonical-invoke-topic resolver, the contract/overlay config loader,
and the dispatch builder. The adapter binds an already-routed invocation command
to the canonical remote-agent invoke transport; it performs no provider/model/
tier/escalation selection.
"""

from omnimarket.adapters.adk.adapter_adk_invoke import (
    build_adk_invocation_dispatch,
    load_adk_invoke_config,
    resolve_adk_invoke_topic,
)
from omnimarket.adapters.adk.models import (
    ModelAdkInvocationDispatch,
    ModelAdkInvokeConfig,
    ModelAdkRunnerBinding,
)

__all__ = [
    "ModelAdkInvocationDispatch",
    "ModelAdkInvokeConfig",
    "ModelAdkRunnerBinding",
    "build_adk_invocation_dispatch",
    "load_adk_invoke_config",
    "resolve_adk_invoke_topic",
]
