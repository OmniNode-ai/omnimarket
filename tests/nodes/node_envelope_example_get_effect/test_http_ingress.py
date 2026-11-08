# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Behaviour of the GET example-envelope route on the mounted app."""

from __future__ import annotations

import pytest

from tests.nodes import envelope_example_support as support

pytestmark = pytest.mark.unit
ROUTE = support.GET_ROUTE


def test_contract_binds_exactly_this_route() -> None:
    support.check_contract_binds_exactly_this_route(ROUTE)


def test_two_contracts_cover_the_two_routes_without_overlap() -> None:
    support.check_two_contracts_cover_the_two_routes_without_overlap()


def test_handler_binding_names_the_importable_handler() -> None:
    support.check_handler_binding_names_the_importable_handler(ROUTE)


def test_serves_the_example_envelope() -> None:
    support.check_serves_the_example_envelope(ROUTE)


def test_each_call_is_a_fresh_example() -> None:
    support.check_each_call_is_a_fresh_example(ROUTE)


def test_anonymous_and_stateless() -> None:
    support.check_anonymous_and_stateless(ROUTE)


def test_other_methods_and_paths_are_not_served() -> None:
    support.check_other_methods_and_paths_are_not_served(ROUTE)


def test_docs_are_not_published() -> None:
    support.check_docs_are_not_published(ROUTE)
