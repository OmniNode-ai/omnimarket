# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind the queue's registered emit verb from its installed contract."""

import importlib
from importlib.metadata import entry_points
from pathlib import Path
from typing import cast

import yaml

from omnimarket.protocols.protocol_error_rule_match import (
    ProtocolErrorRuleExceptionEmitter,
)


def bind_exception_emit(node_name: str) -> ProtocolErrorRuleExceptionEmitter:
    (entry,) = entry_points(group="onex.nodes", name=node_name)
    package = importlib.import_module(entry.value)
    if package.__file__ is None:
        raise ValueError("queue node must have an installed contract")
    contract = yaml.safe_load(
        Path(package.__file__).with_name("contract.yaml").read_text()
    )
    (route,) = [
        item
        for item in contract["handler_routing"]["handlers"]
        if item.get("operation") == "emit"
    ]
    spec = route["handler"]
    factory = getattr(importlib.import_module(spec["module"]), spec["name"])
    return cast("ProtocolErrorRuleExceptionEmitter", factory())
