# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCodeEvalTrialOrchestrator: the code eval trial run's orchestrator (OMN-23627).

Exemplar: ``nodes/node_pr_landing_orchestrator/handlers/handler_pr_landing_orchestrator.py``.
One handler instance serves every route of the contract; each consumed message is
one leg against the ``state_io`` row keyed by the run's correlation id. The leg
logic is :func:`...orchestration.leg.run_leg` (an orchestrator composes, the pure
reducer decides, the lab work unit effect only executes a typed command and
reports; operator RULING 2026-11-11T19:50:17Z). This class binds the leg to a row
store under compare-and-set with retry, holds the newest capacity readings and
the ids of the open runs the tick route nudges. No route waits.

Differs from the exemplar: the capacity, tick and closed routes carry no row
(they hold process-local readings), and a configuration error from the run leg
(timeouts plus slack that do not fit the completion bound) is raised, not
caught, so the runtime dead-letters the request and writes no row.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_ingress import (
    run_id_of,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_closed import (
    ModelCodeEvalRunClosed,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_row import (
    ModelCodeEvalRunRow,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.leg import (
    CodeEvalLegConfig,
    CodeEvalLegPorts,
    CodeEvalLegResult,
    nudges_for,
    run_leg,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.row_store import (
    InMemoryCodeEvalRowStore,
    ProtocolCodeEvalRowStore,
    read_active_state_io_rows,
    run_with_cas_retry,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.state_codec import (
    shared_state_io_store,
)

logger = logging.getLogger(__name__)


class HandlerCodeEvalTrialOrchestrator:
    """One leg per consumed message, under the row's compare-and-set."""

    def __init__(
        self,
        *,
        config: CodeEvalLegConfig | None = None,
        ports: CodeEvalLegPorts | None = None,
        store: ProtocolCodeEvalRowStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._ports = ports
        self._local_store: ProtocolCodeEvalRowStore = (
            store if store is not None else InMemoryCodeEvalRowStore()
        )
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._open_runs: set[UUID] = set()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["ORCHESTRATOR"]:
        return "ORCHESTRATOR"

    def _store(self) -> ProtocolCodeEvalRowStore:
        if read_active_state_io_rows() is not None:
            return shared_state_io_store()
        return self._local_store

    def _leg_config(self) -> CodeEvalLegConfig:
        if self._config is None:
            from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.contract_config import (
                load_leg_config,
            )

            self._config = load_leg_config()
        return self._config

    def _leg_ports(self) -> CodeEvalLegPorts:
        if self._ports is None:
            from omnimarket.nodes.node_code_eval_trial_orchestrator.container.container_inmemory import (
                bind_ports,
            )

            self._ports = bind_ports()
        return self._ports

    async def handle(self, request: BaseModel) -> list[BaseModel]:
        """Apply one message to its run's row; return what to publish."""
        if isinstance(request, ModelHostCapacityAdvertisement):
            self._leg_ports().free_hosts.record(request)
            return []
        if isinstance(request, ModelRuntimeTick):
            return list(
                nudges_for(
                    frozenset(self._open_runs), request.now, str(request.tick_id)
                )
            )
        if isinstance(request, ModelCodeEvalRunClosed):
            self._open_runs.discard(request.correlation_id)
            return []
        run_id = run_id_of(request)
        if run_id is None:
            logger.info("[CODE-EVAL] %s names no run", type(request).__name__)
            return []
        config, ports, now = self._leg_config(), self._leg_ports(), self._clock()

        async def decide(
            row: ModelCodeEvalRunRow | None,
        ) -> tuple[ModelCodeEvalRunRow | None, CodeEvalLegResult]:
            result = run_leg(row, request, now=now, config=config, ports=ports)
            # A leg that changes nothing writes nothing, so the row's updated_at stands.
            return (result.row if result.row_changed else None), result

        result = await run_with_cas_retry(self._store(), str(run_id), decide)
        if result.dropped:
            logger.info(
                "[CODE-EVAL] %s %s dropped: %s",
                run_id,
                type(request).__name__,
                result.transition,
            )
        if result.row is not None and not result.row.closed:
            self._open_runs.add(run_id)
        else:
            self._open_runs.discard(run_id)
        return list(result.emitted)

    async def handle_async(self, request: BaseModel) -> ModelHandlerOutput[None]:
        """Runtime entrypoint: the leg's emissions as publishable handler output."""
        events = await self.handle(request)
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=run_id_of(request) or uuid4(),
            handler_id="node_code_eval_trial_orchestrator.run",
            events=tuple(events),
        )


__all__ = ["HandlerCodeEvalTrialOrchestrator"]
