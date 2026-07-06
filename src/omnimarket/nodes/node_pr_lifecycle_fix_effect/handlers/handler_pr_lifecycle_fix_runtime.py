# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrLifecycleFixRuntime — runtime-boot construction with live OCC adapters.

The ONEX runtime discovers this node from its ``contract.yaml`` ``handler_routing``
and instantiates the declared handler via ``ServiceHandlerResolver`` with **zero
constructor arguments** — every parameter on the base :class:`HandlerPrLifecycleFix`
has a default, so the resolver lands on its zero-arg construction path. Under that
path the base class defaults every adapter to a ``_Noop*`` stub, so a born-path
``onex.cmd.omnimarket.occ-autobind.v1`` command consumed on the .201 effects
runtime would report ``fix_applied=True`` while doing nothing (OMN-13990, §1.2
defect 3).

This subclass is the runtime-boot construction surface: it default-constructs the
**live** OCC adapters (:class:`OccAutobindAdapter`, :class:`OccContractAdapter`)
so the ``receipt_evidence_source_autobind`` and ``deploy_gate_contract_not_found``
block reasons — the only two carried by this node's ``subscribe_topics`` on the
runtime consume path — perform real OCC companion authoring. It mirrors the
live-adapter wiring the merge-sweep orchestrator injects in
``HandlerPrLifecycleOrchestrator._ensure_sub_handlers`` (§4 item 2).

The remaining adapters (GitHub rerun, agent dispatch, delegated fix, two-strike
store) keep their base noop/in-memory defaults deliberately: those block reasons
are driven by the in-process merge-sweep tick, not the runtime consume path, and
their live wiring pulls in worktree/agent machinery inappropriate for an effects
container. Explicit constructor overrides are always honoured (tests inject
mocks; the resolver may inject known params).

The zero-required-parameter constructor keeps this class boot-resolvable
(``test_handler_routing_boot_resolvable``): it has no non-injectable required
ctor param, so the resolver's zero-arg path constructs it cleanly.

Ticket: OMN-13990 (drive the OCC emitter at the normal/born path).
"""

from __future__ import annotations

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_autobind import (
    OccAutobindAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_contract import (
    OccContractAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_two_strike_store import (
    ProtocolTwoStrikeStore,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
    ProtocolAgentDispatchAdapter,
    ProtocolDelegationFixAdapter,
    ProtocolGitHubAdapter,
    ProtocolOccAutobindAdapter,
    ProtocolOccContractAdapter,
)


class HandlerPrLifecycleFixRuntime(HandlerPrLifecycleFix):
    """Runtime-boot :class:`HandlerPrLifecycleFix` with live OCC adapters by default.

    Behaviourally identical to the base handler except that a bare
    ``HandlerPrLifecycleFixRuntime()`` (the shape the runtime resolver constructs)
    binds the **live** OCC contract + autobind adapters instead of no-ops.
    """

    def __init__(
        self,
        github_adapter: ProtocolGitHubAdapter | None = None,
        agent_dispatch_adapter: ProtocolAgentDispatchAdapter | None = None,
        occ_contract_adapter: ProtocolOccContractAdapter | None = None,
        occ_autobind_adapter: ProtocolOccAutobindAdapter | None = None,
        delegation_fix_adapter: ProtocolDelegationFixAdapter | None = None,
        two_strike_store: ProtocolTwoStrikeStore | None = None,
        delegation_model_name: str = "ruff-deterministic",
    ) -> None:
        super().__init__(
            github_adapter=github_adapter,
            agent_dispatch_adapter=agent_dispatch_adapter,
            occ_contract_adapter=occ_contract_adapter or OccContractAdapter(),
            occ_autobind_adapter=occ_autobind_adapter or OccAutobindAdapter(),
            delegation_fix_adapter=delegation_fix_adapter,
            two_strike_store=two_strike_store,
            delegation_model_name=delegation_model_name,
        )


__all__ = ["HandlerPrLifecycleFixRuntime"]
