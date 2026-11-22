# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the Docker engine CPUs and memory through omnibase_infra local engine reader."""

from __future__ import annotations

from uuid import uuid4

from omnibase_core.models.container.model_onex_container import ModelONEXContainer


async def read_engine_capacity() -> tuple[float, int]:
    # Older supported infra builds lack this optional local-stack operation.
    from omnibase_infra.handlers.handler_local_engine_info import (
        HandlerLocalEngineInfo,
    )
    from omnibase_infra.models.model_local_engine_info_request import (
        ModelLocalEngineInfoRequest,
    )

    owner = HandlerLocalEngineInfo(ModelONEXContainer())
    await owner.initialize({})
    try:
        result = await owner.handle_async(
            ModelLocalEngineInfoRequest(correlation_id=uuid4(), timeout_seconds=20)
        )
    finally:
        await owner.shutdown()
    if result.engine_status == "unavailable":
        raise OSError("Docker observation unavailable")
    if (
        result.engine_status != "read"
        or result.engine_cpus is None
        or result.engine_memory_bytes is None
    ):
        raise ValueError("Invalid engine resources")
    return float(result.engine_cpus), result.engine_memory_bytes
