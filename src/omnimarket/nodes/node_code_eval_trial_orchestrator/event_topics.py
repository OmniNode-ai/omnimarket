# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Frozen class-name to topic routing for the code eval trial's own events.

The delegation canary routes typed terminals by class name through its
contract's ``published_events`` block. This map is that block's form: one
entry per published event, with ``event_type`` the class name minus its
``Model`` prefix, and a conformance test holds the two equal from then on.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from omnimarket.events.topics import CODE_EVAL_RUN_CLOSED_TOPIC_V1
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_closed import (
    ModelCodeEvalRunClosed,
)

CODE_EVAL_EVENT_TOPICS: Mapping[type[BaseModel], str] = {
    ModelCodeEvalRunClosed: CODE_EVAL_RUN_CLOSED_TOPIC_V1,
}


__all__: list[str] = ["CODE_EVAL_EVENT_TOPICS"]
