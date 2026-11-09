# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: values come from the contract and its overlay, nothing else."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.nodes import envelope_example_support as support

pytestmark = pytest.mark.unit
ROUTE = support.POST_ROUTE


def test_overlay_values_reach_the_response() -> None:
    support.check_overlay_values_reach_the_response(ROUTE)


def test_installed_overlay_reaches_the_no_argument_factory() -> None:
    support.check_installed_overlay_reaches_the_no_argument_factory(ROUTE)


def test_environment_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    support.check_environment_changes_nothing(ROUTE, monkeypatch)


def test_partial_overlay_overrides_only_what_it_names() -> None:
    support.check_partial_overlay_overrides_only_what_it_names(ROUTE)


def test_broken_overlay_refuses_to_build() -> None:
    support.check_broken_overlay_refuses_to_build(ROUTE)


def test_contract_settings_missing_refuses_to_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    support.check_contract_settings_missing_refuses_to_build(
        ROUTE, monkeypatch, tmp_path
    )


def test_mismatched_handler_binding_refuses_to_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    support.check_mismatched_handler_binding_refuses_to_build(
        ROUTE, monkeypatch, tmp_path
    )


def test_no_environment_read_and_no_literal_values() -> None:
    support.check_no_environment_read_and_no_literal_values(ROUTE)
