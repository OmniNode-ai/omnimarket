# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install input preserving the experiment arm across a JSON/bus round trip."""

from omnibase_core.models.claude_code_binding import (
    ModelBindingInstallRequest,
    ModelClientBinding,
)

from omnimarket.models.claude_code_binding import ModelCompactionClientBinding


class ModelCompactionBindingInstallRequest(ModelBindingInstallRequest):
    bindings: tuple[ModelCompactionClientBinding | ModelClientBinding, ...]
