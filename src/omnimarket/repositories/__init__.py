# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared repository implementations for omnimarket nodes.

Repositories live here rather than inside a node package because more than one
node consumes them, and a node must never import another node's private
modules (omnimarket CLAUDE.md).
"""

from omnimarket.repositories.repository_code_entity_postgres import (
    RepositoryCodeEntityPostgres,
)

__all__ = ["RepositoryCodeEntityPostgres"]
