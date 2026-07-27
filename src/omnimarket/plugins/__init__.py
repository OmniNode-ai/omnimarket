# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnimarket domain plugins.

Domain plugins are the kernel's in-repo bootstrap hook: the service kernel
discovers them from the ``onex.domain_plugins`` entry-point group and calls
their lifecycle methods with a config carrying the boot ``ModelONEXContainer``.
This is the only seam by which an omnimarket-owned service can be registered in
the boot container without omnibase_infra importing omnimarket (which the
compat -> core -> spi -> infra layering forbids).
"""

from omnimarket.plugins.plugin_code_entity_repository import (
    PluginCodeEntityRepository,
)

__all__ = ["PluginCodeEntityRepository"]
