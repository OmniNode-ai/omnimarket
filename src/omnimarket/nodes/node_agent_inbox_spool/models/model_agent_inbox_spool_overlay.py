# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment facts supplied explicitly or through the structured overlay."""

from typing import Annotated

from pydantic import Field, StringConstraints

from ._base import ModelFrozen

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ModelAgentInboxSpoolOverlay(ModelFrozen):
    # One broker group per host: {consumer_group_prefix}-{host}.
    consumer_group_prefix: NonEmpty
    # Directory that holds one sub-directory per lane on this host.
    spool_root: NonEmpty
    read_window_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    # The caller uses this lane when constructing the injected bus.
    bus_lane: NonEmpty
