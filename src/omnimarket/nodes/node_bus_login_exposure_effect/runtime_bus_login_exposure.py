# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI of node_bus_login_exposure_effect: run one probe from the overlay, print the result.

Exit 0 when the login sees nothing the overlay does not allow, 1 when it sees an extra topic or
group (an exposure), 2 when the view was not read or is stale.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_bus_login_exposure_effect.handlers.handler_bus_login_exposure import (
    HandlerBusLoginExposure,
)
from omnimarket.nodes.node_bus_login_exposure_effect.models.model_bus_login_exposure import (
    ModelBusLoginExposureRequest,
)

_EXIT = {
    EnumReadingVerdict.WORKING: 0,
    EnumReadingVerdict.NOT_WORKING: 1,
    EnumReadingVerdict.UNKNOWN: 2,
}


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    handler = HandlerBusLoginExposure()
    result = handler.handle(
        ModelBusLoginExposureRequest(
            correlation_id=str(uuid.uuid4()), now=datetime.now(UTC)
        )
    )
    topic, payload = handler.event_for(result)
    sys.stdout.write(
        json.dumps({"topic": topic, "result": payload}, sort_keys=True) + "\n"
    )
    return _EXIT[result.reading.verdict]
