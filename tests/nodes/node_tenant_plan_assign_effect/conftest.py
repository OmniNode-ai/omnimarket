# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scratch PostgreSQL 16 fixture is the tenant provision node's: one place builds the owned container."""

from ..node_tenant_provision_effect.conftest import port

__all__ = ["port"]
