# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from pydantic import BaseModel


class LiveRunnerConfig(BaseModel):
    config_id: str
    runner_name: str
    settings: dict
    updated_at: str
