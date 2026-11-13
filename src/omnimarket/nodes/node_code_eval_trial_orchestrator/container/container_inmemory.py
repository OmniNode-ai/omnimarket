# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the ports the code eval orchestrator runs on when none is injected (OMN-23627).

Exemplar: ``nodes/node_pr_landing_orchestrator/container/container_inmemory.py``.
The handler never constructs a port itself: it takes the ones this module binds.
The real profile reads the pool's capacity advertisements and the canonical clones;
tests inject the in-memory suite reader and a static host list.
"""

from __future__ import annotations

from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.contract_config import (
    max_hosts,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.free_hosts import (
    PlacedFreeHosts,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.leg import (
    CodeEvalLegPorts,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.ports import (
    ProtocolCodeEvalFreeHosts,
    ProtocolCodeEvalSuiteReader,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.suite_reader import (
    GitCodeEvalSuiteReader,
)
from omnimarket.nodes.node_lab_fill_orchestrator.handlers.handler_lab_fill_host_readings import (
    LabHostReadings,
)


def bind_ports(
    *,
    suite_reader: ProtocolCodeEvalSuiteReader | None = None,
    free_hosts: ProtocolCodeEvalFreeHosts | None = None,
) -> CodeEvalLegPorts:
    """Bind the real ports for any the caller did not inject."""
    return CodeEvalLegPorts(
        suite_reader=suite_reader
        if suite_reader is not None
        else GitCodeEvalSuiteReader.from_overlays(),
        free_hosts=free_hosts
        if free_hosts is not None
        else PlacedFreeHosts(LabHostReadings(max_hosts())),
    )


__all__ = ["bind_ports"]
