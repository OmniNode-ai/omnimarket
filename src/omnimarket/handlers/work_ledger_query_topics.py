# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve command and terminal topics for the existing work-ledger query."""

from importlib import resources

import yaml

from omnimarket.models.scm import ModelScmTopics


def work_ledger_query_topics() -> ModelScmTopics:
    """The work-ledger query node's command and terminal topics, as its contract declares them."""
    package = resources.files("omnimarket.nodes.node_work_ledger_query_effect")
    contract = yaml.safe_load((package / "contract.yaml").read_text(encoding="utf-8"))
    dispatch = contract["runtime_dispatch"]
    return ModelScmTopics(
        command=str(dispatch["command_topic"]),
        success=str(dispatch["terminal_events"]["success"]),
    )
