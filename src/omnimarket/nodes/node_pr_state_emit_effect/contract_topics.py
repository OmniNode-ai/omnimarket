# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic and event type authority is the emit contract."""

from pathlib import Path

import yaml

from omnimarket.nodes.contract_topics import contract_publish_topics

CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"
TOPIC_PR_STATE_OBSERVED = contract_publish_topics(CONTRACT_PATH)[0]
with CONTRACT_PATH.open(encoding="utf-8") as handle:
    _event_type: object = yaml.safe_load(handle)["event_type"]
if not isinstance(_event_type, str) or not _event_type:
    raise ValueError("emit contract must declare event_type")
EVENT_TYPE: str = _event_type
