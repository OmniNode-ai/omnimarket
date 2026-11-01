# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""EFFECT boundary of the announce step: announce, build the catalog, report (OMN-21597).

The handler takes the lane overlay as its request and resolves the registry of
bindings from the container it is built with, which is how the runtime wires a node
host. Which bus, inventory store and distribution source it uses is the overlay's
choice of binding names, so the same step runs on the in-memory bus and on a durable
bus. Step 0, which runs before any container exists, calls ``AnnounceStep0`` directly
through ``runtime_package_announce``.
"""

from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.models.container.model_onex_container import ModelONEXContainer

from omnimarket.nodes.node_package_announce_effect.announce_bindings import (
    AnnounceBindings,
)
from omnimarket.nodes.node_package_announce_effect.announce_step0 import (
    AnnounceStep0,
)
from omnimarket.nodes.node_package_announce_effect.models.model_node_announce_overlay import (
    ModelNodeAnnounceOverlay,
)
from omnimarket.nodes.node_package_announce_effect.models.model_package_announce_result import (
    ModelPackageAnnounceResult,
)


class HandlerPackageAnnounce:
    """Run boot step 0 for one overlay and return what it did."""

    def __init__(self, container: ModelONEXContainer) -> None:
        self._container = container

    async def handle(
        self, request: ModelNodeAnnounceOverlay
    ) -> ModelPackageAnnounceResult:
        """Announce every installed contract and return the report and catalog snapshot."""
        step0 = AnnounceStep0(
            overlay=request, bindings=self._container.get_service(AnnounceBindings)
        )
        catalog = await step0.run()
        report = step0.report
        if report is None:
            raise ModelOnexError(
                "announce step finished without a report",
                error_code=EnumCoreErrorCode.INTERNAL_ERROR,
            )
        return ModelPackageAnnounceResult(report=report, entries=catalog.snapshot())
