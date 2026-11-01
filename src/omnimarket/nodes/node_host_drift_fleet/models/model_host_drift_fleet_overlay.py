# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment facts supplied explicitly or through the structured overlay."""

from typing import Annotated

from pydantic import Field, StringConstraints

from ._base import ModelFrozen

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ModelHostDriftFleetOverlay(ModelFrozen):
    hosts: tuple[NonEmpty, ...]
    read_only_hosts: tuple[NonEmpty, ...]
    heartbeat_stale_seconds: Annotated[int, Field(gt=0)]
    consumer_group_prefix: NonEmpty
    read_window_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    bus_lane: NonEmpty
