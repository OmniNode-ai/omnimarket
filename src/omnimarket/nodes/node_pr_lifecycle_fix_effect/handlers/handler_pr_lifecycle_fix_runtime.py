# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime wrapper for product-repository PR remediation.

The wrapper preserves the canonical output and duplicate-delivery handling.
Legacy companion commands are refused by the wrapped handler; no writer is
constructed during runtime boot.
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
    """Wrap normal PR fixes and remember completed command deliveries."""

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
        self._fix = HandlerPrLifecycleFix(
            github_adapter=github_adapter,
            agent_dispatch_adapter=agent_dispatch_adapter,
            occ_contract_adapter=occ_contract_adapter,
            occ_autobind_adapter=occ_autobind_adapter,
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
