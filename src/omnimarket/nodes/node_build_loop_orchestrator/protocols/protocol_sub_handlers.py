# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from typing import Protocol


class AdapterGitHubBridge(Protocol):
    def handle_github_webhook(self, payload: dict) -> None: ...

    def send_pipeline_update(self, event_data: dict) -> None: ...
