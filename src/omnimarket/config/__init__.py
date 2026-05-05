# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnimarket.config — typed Settings for omnimarket production code.

OMN-10548. The single canonical config layer. All production code that needs
configuration reads it through `Settings` (or `BindingConfigResolver` /
`ProtocolSecretStore` once Epic 2-3 land). Raw `os.environ` is exception-only
and must carry a ticketed annotation (see plan Task 3).
"""

from omnimarket.config.settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]
