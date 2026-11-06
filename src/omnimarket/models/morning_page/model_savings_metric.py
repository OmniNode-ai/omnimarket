# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One delegation-savings number of the status page, with the exposure it came from."""

from pydantic import BaseModel, ConfigDict


class ModelSavingsMetric(BaseModel):
    """One delegation-savings number, always carrying the exposure it came from."""

    model_config = ConfigDict(frozen=True)

    label: str
    value: str
    source_topic: str
