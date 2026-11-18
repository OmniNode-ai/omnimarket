# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plan first slice compute: whether a plan in review names its first slice section.

``HandlerPlanFirstSlice.handle(ModelPlanFirstSliceRequest) -> ModelPlanFirstSliceResult``
(definition-B). It takes the plan's first markdown heading that names a first slice
section (the names come from the request's settings, which the overlay fills), reads
the section's lines up to the next same-or-higher heading or a later-rung heading,
and counts the section's words against the minimum.
"""

from omnimarket.nodes.node_plan_first_slice_compute.handlers.handler_plan_first_slice import (
    HandlerPlanFirstSlice,
    check_plan_first_slice,
)


class NodePlanFirstSliceCompute(HandlerPlanFirstSlice):
    """ONEX entry-point wrapper for HandlerPlanFirstSlice."""


__all__ = [
    "HandlerPlanFirstSlice",
    "NodePlanFirstSliceCompute",
    "check_plan_first_slice",
]
