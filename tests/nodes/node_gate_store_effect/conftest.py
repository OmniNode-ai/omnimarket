# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registers the integration database fixture for this directory's tests."""

from tests.nodes.node_gate_store_effect.support_postgres import database

integration_database = database
