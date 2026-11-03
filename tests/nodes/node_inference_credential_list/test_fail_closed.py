# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: the list route refuses before any secret write, bus publish or projection read."""

from __future__ import annotations

import pytest

from tests.nodes import fail_closed_cases as cases

pytestmark = pytest.mark.unit


def test_a_caller_with_no_resolved_tenant_is_refused_with_no_adapter_call() -> None:
    cases.assert_unauthenticated_refused("list")


def test_an_unbound_identity_resolver_refuses_every_request_with_no_adapter_call() -> (
    None
):
    cases.assert_unbound_refused("list")
