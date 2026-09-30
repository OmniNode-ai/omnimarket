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
single **live** OCC producer (:class:`OccCompanionEmitter`, OMN-14285) into both
OCC slots so the ``receipt_evidence_source_autobind`` and
``deploy_gate_contract_not_found`` block reasons — the only two carried by this
node's ``subscribe_topics`` on the runtime consume path — perform real OCC
companion authoring through ONE writer. It mirrors the live wiring the merge-sweep
orchestrator injects in ``HandlerPrLifecycleOrchestrator._ensure_sub_handlers``.

The remaining adapters (GitHub rerun, agent dispatch, delegated fix, two-strike
store) keep their base noop/in-memory defaults deliberately: those block reasons
are driven by the in-process merge-sweep tick, not the runtime consume path, and
their live wiring pulls in worktree/agent machinery inappropriate for an effects
container. Explicit constructor overrides are always honoured (tests inject
mocks; the resolver may inject known params).

The zero-required-parameter constructor keeps this class boot-resolvable
(``test_handler_routing_boot_resolvable``): it has no non-injectable required
ctor param, so the resolver's zero-arg path constructs it cleanly.

OMN-19832 (wave-2 task T10 of the PR landing workflow): this is also where the
typed companion outcome leaves the node. For an autobind command, ``handle``
returns a ``ModelHandlerOutput`` carrying two events: the unchanged
``ModelPrLifecycleFixResult`` (routed to the terminal
``onex.evt.omnimarket.pr-lifecycle-fix-completed.v1``) and the
``ModelPrLandingCompanionOutcome`` (routed by the contract's
``published_events`` map to ``onex.evt.omnimarket.pr-landing-companion-outcome.v1``).
Every other block reason returns the result alone, as before. The class wraps
:class:`HandlerPrLifecycleFix` rather than subclassing it, because its ``handle``
now returns a different type from the base handler's, which the merge-sweep
tick keeps calling directly.

Ticket: OMN-13990 (drive the OCC emitter at the normal/born path).
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from uuid import UUID, uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

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
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    ModelPrLifecycleFixCommand,
)

_HANDLER_ID = "node_pr_lifecycle_fix_effect"

logger = logging.getLogger(__name__)

# What makes two deliveries the same command: a dead-letter replay or a
# producer retry repeats every one of these; a new request mints a fresh
# correlation id (OMN-20119).
type _CommandIdentity = tuple[str, str | None, str, int, str, str | None]


def _identity(command: ModelPrLifecycleFixCommand) -> _CommandIdentity:
    return (
        str(command.correlation_id),
        command.command_id,
        command.repo,
        command.pr_number,
        str(command.block_reason),
        None if command.op is None else str(command.op),
    )


class HandlerPrLifecycleFixRuntime:
    """Runtime-boot :class:`HandlerPrLifecycleFix` with live OCC adapters by default.

    Behaviourally identical to the base handler except that a bare
    ``HandlerPrLifecycleFixRuntime()`` (the shape the runtime resolver constructs)
    binds the single **live** :class:`OccCompanionEmitter` into both OCC slots
    instead of no-ops (OMN-14285: one producer, both failure classes), and that
    an autobind command also yields the typed companion outcome (OMN-19832).

    OMN-20119: the command topic has a second consumer group (the PR landing
    orchestrator), and a dead-letter replay of its failures writes copies back
    to the shared topic. A copy of a command this instance already completed is
    answered with the output it produced, and logged loudly, instead of running
    the companion authoring and the product-PR marker again. Only runs that
    completed without an error are remembered (a decline such as a skip counts
    as completed), so a run that raised or errored is retried as before, and the
    memory is bounded to :attr:`COMPLETED_MEMORY` commands.
    """

    COMPLETED_MEMORY = 1024

    def __init__(
        self,
        github_adapter: ProtocolGitHubAdapter | None = None,
        agent_dispatch_adapter: ProtocolAgentDispatchAdapter | None = None,
        occ_contract_adapter: ProtocolOccContractAdapter | None = None,
        occ_autobind_adapter: ProtocolOccAutobindAdapter | None = None,
        delegation_fix_adapter: ProtocolDelegationFixAdapter | None = None,
        two_strike_store: ProtocolTwoStrikeStore | None = None,
        delegation_model_name: str = "ruff-deterministic",
        outcome_token_resolver: Callable[[], str | None] | None = None,
        head_sha_resolver: Callable[[str, int, str | None], str | None] | None = None,
    ) -> None:
        # One producer serves both OCC failure classes (OMN-14285). A single
        # emitter instance is shared across both slots so the deploy-gate and
        # autobind reasons resolve to identical authoring behavior.
        emitter = OccCompanionEmitter()
        self._fix = HandlerPrLifecycleFix(
            github_adapter=github_adapter,
            agent_dispatch_adapter=agent_dispatch_adapter,
            occ_contract_adapter=occ_contract_adapter or emitter,
            occ_autobind_adapter=occ_autobind_adapter or emitter,
            delegation_fix_adapter=delegation_fix_adapter,
            two_strike_store=two_strike_store,
            delegation_model_name=delegation_model_name,
            outcome_token_resolver=outcome_token_resolver,
            head_sha_resolver=head_sha_resolver,
        )
        self._completed: OrderedDict[_CommandIdentity, tuple[object, ...]] = (
            OrderedDict()
        )
        self._duplicates = 0

    @property
    def fix_handler(self) -> HandlerPrLifecycleFix:
        """The wrapped handler, with the adapters this runtime bound."""
        return self._fix

    async def handle(
        self, command: ModelPrLifecycleFixCommand
    ) -> ModelHandlerOutput[None]:
        """Run the fix; emit its result and, for autobind, the companion outcome."""
        identity = _identity(command)
        events = self._completed.get(identity)
        if events is not None:
            self._completed.move_to_end(identity)
            self._duplicates += 1
            logger.warning(
                "PR lifecycle fix: duplicate delivery of a completed command, "
                "answered without a second run (OMN-20119): pr=%s repo=%s "
                "reason=%s op=%s correlation_id=%s command_id=%s "
                "duplicates_answered=%d",
                command.pr_number,
                command.repo,
                command.block_reason,
                command.op,
                command.correlation_id,
                command.command_id,
                self._duplicates,
            )
        else:
            result, outcome = await self._fix.handle_with_companion_outcome(command)
            events = (result,) if outcome is None else (result, outcome)
            if result.error is None:
                self._completed[identity] = events
                while len(self._completed) > self.COMPLETED_MEMORY:
                    self._completed.popitem(last=False)
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=command.correlation_id,
            handler_id=_HANDLER_ID,
            events=events,
        )

    @property
    def handler_type(self) -> str:
        return self._fix.handler_type

    @property
    def handler_category(self) -> str:
        return self._fix.handler_category

    @property
    def correlation_id(self) -> UUID | None:
        return self._fix.correlation_id


__all__ = ["HandlerPrLifecycleFixRuntime"]
