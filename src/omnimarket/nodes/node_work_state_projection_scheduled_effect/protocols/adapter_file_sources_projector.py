# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend profile: the effect node's file sources, projected and written where the floor and the fill read."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime

from omnimarket.models.work_state_projection import ModelWorkStateResult
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
    ProtocolLedgerRowSource,
)
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import (
    publish_projection,
    run,
)

logger = logging.getLogger(__name__)


class UnroutedLedgerRowSource:
    """A ledger read the overlay's command route could not configure; every read answers the same refusal naming the cause."""

    def __init__(self, cause: str) -> None:
        self._cause = cause

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        return ModelLedgerRead(error=f"ledger-route-unconfigured: {self._cause}")


def publish_after_projection(
    result: ModelWorkStateResult, env: Mapping[str, str]
) -> str:
    """Publish the projection as the work_state floor projection, as the node's own CLI does after its run.

    The answer says what happened (``published``, ``not published``, ``not configured`` when no publish overlay names a bus).
    A bus that refuses is a reading, not a failure of the run: the projection file is already written, the floor reads
    the published projection as it ages and unknown, and the next tick publishes again.
    """
    try:
        answer = publish_projection(result, env, datetime.now(UTC))
    except Exception as exc:
        answer = f"not published ({type(exc).__name__}: {exc})"
        logger.warning("work-state projection %s", answer)
        return answer
    logger.info("work-state projection %s", answer)
    return answer


class FileSourcesWorkStateProjector:
    """Runs node_work_state_projection_effect's read, project and atomic write once per call, then publishes the projection; when a state home is set, the run's OMNI_HOME points at it instead of the process environment's."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        state_home: str | None = None,
        ledger_read: ModelCommandLedgerReadRoute | None = None,
    ) -> None:
        self._environ = environ
        self._state_home = state_home
        self._ledger_read = ledger_read

    def project(self, now: datetime) -> ModelWorkStateResult:
        env = dict(os.environ if self._environ is None else self._environ)
        if self._state_home is not None:
            env["OMNI_HOME"] = self._state_home
        if self._ledger_read is not None:
            try:
                source: ProtocolLedgerRowSource = build_row_source(
                    self._ledger_read, env
                )
            except LedgerRouteError as exc:
                source = UnroutedLedgerRowSource(str(exc))
            result = run(env, now, ledger_source=source)
        else:
            result = run(env, now)
        publish_after_projection(result, env)
        return result
