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
from pathlib import Path

import yaml
from pydantic import BaseModel

from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_closed import (
    ModelCodeEvalRunClosed,
)

_CONTRACT = yaml.safe_load(
    (Path(__file__).parent / "contract.yaml").read_text(encoding="utf-8")
)

# The topic is read from the node's own contract (its terminal event), so no topic
# literal lives in code and the map cannot drift from the contract.
CODE_EVAL_EVENT_TOPICS: Mapping[type[BaseModel], str] = {
    ModelCodeEvalRunClosed: str(_CONTRACT["terminal_event"]),
}


__all__: list[str] = ["CODE_EVAL_EVENT_TOPICS"]
