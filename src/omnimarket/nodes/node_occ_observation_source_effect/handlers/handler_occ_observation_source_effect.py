# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOccObservationSourceEffect — read the durable OCC observation trail (OMN-14888).

This is the concrete answer to "point the dedup projection at OCC as its
source": it walks a local ``onex_change_control`` checkout's
``drift/occ_observations/`` tree, parses each file back into a
:class:`ModelOccObservationRecord`, and hands the raw log — completely
unmodified — to the EXISTING ``project_qualifying_observations`` (OMN-14851).
No change to that function's dedup/aggregation semantics; this node is purely
the disk-read boundary in front of it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from omnimarket.events.occ_observation_record import (
    ModelOccObservationRecord,
    project_qualifying_observations,
)
from omnimarket.events.occ_observation_store import (
    OCC_OBSERVATIONS_ROOT,
    parse_occ_observation_record,
)
from omnimarket.nodes.node_occ_observation_source_effect.models.model_occ_observation_source_effect_request import (
    ModelOccObservationSourceEffectRequest,
)
from omnimarket.nodes.node_occ_observation_source_effect.models.model_occ_observation_source_effect_result import (
    ModelOccObservationSourceEffectResult,
)

logger = logging.getLogger(__name__)


class HandlerOccObservationSourceEffect:
    """EFFECT handler: read + dedupe the durable OCC observation trail from disk."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(
        self,
        request: ModelOccObservationSourceEffectRequest,
    ) -> ModelOccObservationSourceEffectResult:
        root = Path(request.checkout_dir) / OCC_OBSERVATIONS_ROOT
        records: list[ModelOccObservationRecord] = []
        malformed: list[str] = []

        if root.is_dir():
            for path in sorted(root.rglob("*.yaml")):
                relpath = str(path.relative_to(request.checkout_dir))
                try:
                    records.append(parse_occ_observation_record(path.read_text()))
                except (ValueError, OSError) as exc:
                    logger.warning(
                        "occ_observation_source_effect: malformed record %s: %s",
                        relpath,
                        exc,
                    )
                    malformed.append(relpath)

        observations = project_qualifying_observations(records)
        logger.info(
            "occ_observation_source_effect: %d raw record(s) -> %d distinct source "
            "tuple(s), %d malformed",
            len(records),
            len(observations),
            len(malformed),
        )
        return ModelOccObservationSourceEffectResult(
            observations=observations,
            raw_record_count=len(records),
            distinct_source_tuples=len(observations),
            malformed_paths=tuple(malformed),
        )


__all__ = ["HandlerOccObservationSourceEffect"]
