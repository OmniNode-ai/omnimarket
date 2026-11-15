# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared copies of the lab host table agree on every field of every host (OMN-24558).

The lab host table lives in several copies, and the copies and their paths are the drift node's
overlay's, nothing here names one. This probe runs that node's read in probe mode and maps its
result to a host reading row, so a copy that changed alone is visible beside the host's other
probes. Named break: change one field of one host in one declared copy, or add a copy that
differs. It reads only: it never writes a row of the drift node, a gate, a stop or an admission.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.models.model_host_reading import EnumHostProbe, ModelProbeReading
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_host_reading_effect.protocols import (
    ProtocolHostTableCopies,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.handlers.handler_host_table_copies_drift import (
    HandlerHostTableCopiesDrift,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.models import (
    ModelHostTableDriftCommand,
    ModelHostTableDriftResult,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
)

_PROBE = EnumHostProbe.HOST_TABLE_COPIES


def _unknown(reason: str) -> ModelProbeReading:
    return ModelProbeReading(
        probe=_PROBE,
        verdict=EnumReadingVerdict.UNKNOWN,
        outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
        reasons=(reason,),
    )


def _cells(result: ModelHostTableDriftResult) -> dict[str, str]:
    """The measured counts; names and counts only, never a copy's field value."""
    return {
        "copies": str(len(result.copies)),
        "hosts": str(result.hosts_compared),
        "differing_hosts": str(result.differing_hosts),
        "differing_fields": str(len(result.differences)),
    }


def read_host_table_copies(
    copies: ProtocolHostTableCopies,
    host: str,
    clock: Callable[[], datetime],
) -> ModelProbeReading:
    """Run the declared copies' drift read on ``host`` in probe mode and map its result."""
    try:
        overlay = copies.overlay()
    except NodeOverlayNotFoundError:
        return _unknown("drift-overlay-not-found")
    except NodeOverlayError:
        return _unknown("drift-overlay-invalid")
    handler = HandlerHostTableCopiesDrift(
        overlay=overlay,
        host=host,
        source=copies.source(overlay),
        appender=None,
        clock=clock,
    )
    result = handler.handle(
        ModelHostTableDriftCommand(
            correlation_id=f"host-table-copies-{host}",
            write=False,
        )
    )
    if result.status == "failed":
        return _unknown("drift-run-failed")
    if result.verdict is EnumReadingVerdict.UNKNOWN:
        return ModelProbeReading(
            probe=_PROBE,
            verdict=EnumReadingVerdict.UNKNOWN,
            outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
            reasons=result.reasons,
            cells={"copies": str(len(result.copies))},
        )
    reasons = list(result.reasons)
    if result.verdict is EnumReadingVerdict.NOT_WORKING:
        limit = overlay.publish.max_named
        reasons.extend(
            f"drift-{reason_token(item.host)}-{reason_token(item.field)}"
            for item in result.differences[:limit]
        )
        if len(result.differences) > limit:
            reasons.append(f"and-{len(result.differences) - limit}-more")
    return ModelProbeReading(
        probe=_PROBE,
        verdict=result.verdict,
        outcome=EnumReadingOutcome.READ,
        reasons=tuple(reasons),
        cells=_cells(result),
    )
