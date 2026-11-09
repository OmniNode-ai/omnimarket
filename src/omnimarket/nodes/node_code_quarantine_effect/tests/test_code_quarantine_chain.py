# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain tests of the quarantine node, and the named mutations that turn each red (OMN-23144 AC4)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from omnimarket.nodes.node_code_quarantine_effect.models import EnumFailedGate
from omnimarket.nodes.node_code_quarantine_effect.tests import (
    test_code_quarantine_acceptance as acceptance,
)
from omnimarket.nodes.node_code_quarantine_effect.tests.chains import (
    error_chain,
    golden_chain,
)
from omnimarket.nodes.node_code_quarantine_effect.tests.support import (
    MUTATIONS,
    Rig,
    RigFactory,
    mutated_handler_class,
)

pytestmark = pytest.mark.unit

#: The chain each mutation must turn red. Every mutation turns at least one chain red, and each chain at least one.
RED_UNDER: dict[str, str] = {
    "quarantine-without-clock": "golden",
    "delete-files-on-quarantine": "golden",
    "keep-clock-on-restore": "error",
    "skip-build-gate": "error",
    "skip-start-gate": "error",
    "skip-reading-gate": "error",
    "skip-tag-check": "error",
    "skip-catalog-check": "error",
    "remove-before-days": "error",
}


def test_golden_chain(rig: Rig) -> None:
    golden_chain(rig)


def test_error_chain(rig: Rig) -> None:
    error_chain(rig)


def test_chain_mutation_table_covers_every_named_mutation() -> None:
    assert set(RED_UNDER) == set(MUTATIONS)
    assert {"golden", "error"} <= set(RED_UNDER.values())


@pytest.mark.parametrize("mutation", sorted(RED_UNDER))
def test_chain_mutation_turns_its_chain_red(
    make_rig: RigFactory, mutation: str
) -> None:
    mutant = mutated_handler_class(mutation)
    chain = golden_chain if RED_UNDER[mutation] == "golden" else error_chain
    with pytest.raises(AssertionError):
        chain(make_rig(mutant))


def _acceptance_red_cases() -> list[tuple[str, str, Callable[[Rig], None]]]:
    """Each acceptance read of AC1 to AC3, run against a handler with a named break, must fail."""
    return [
        (
            "quarantine_starts_clock",
            "quarantine-without-clock",
            acceptance.test_quarantine_starts_clock,
        ),
        (
            "quarantine_starts_clock",
            "delete-files-on-quarantine",
            acceptance.test_quarantine_starts_clock,
        ),
        (
            "failure_restores_and_resets",
            "keep-clock-on-restore",
            lambda r: acceptance.test_failure_restores_and_resets(
                r, "build", EnumFailedGate.CLEAN_BUILD
            ),
        ),
        (
            "failure_restores_and_resets",
            "skip-build-gate",
            lambda r: acceptance.test_failure_restores_and_resets(
                r, "build", EnumFailedGate.CLEAN_BUILD
            ),
        ),
        (
            "failure_restores_and_resets",
            "skip-start-gate",
            lambda r: acceptance.test_failure_restores_and_resets(
                r, "start", EnumFailedGate.LAB_RUNTIME_START
            ),
        ),
        (
            "failure_restores_and_resets",
            "skip-reading-gate",
            lambda r: acceptance.test_failure_restores_and_resets(
                r, "reading", EnumFailedGate.MISSING_READING
            ),
        ),
        (
            "removal_needs_tag_and_catalog",
            "skip-tag-check",
            acceptance.test_removal_needs_tag_and_catalog,
        ),
        (
            "removal_needs_tag_and_catalog",
            "skip-catalog-check",
            acceptance.test_removal_needs_tag_and_catalog,
        ),
    ]


@pytest.mark.parametrize(
    ("read", "mutation", "run"),
    _acceptance_red_cases(),
    ids=lambda v: v if isinstance(v, str) else "run",
)
def test_chain_mutation_turns_the_acceptance_read_red(
    make_rig: RigFactory,
    read: str,
    mutation: str,
    run: Callable[[Rig], None],
) -> None:
    assert read in {
        "quarantine_starts_clock",
        "failure_restores_and_resets",
        "removal_needs_tag_and_catalog",
    }
    with pytest.raises(AssertionError):
        run(make_rig(mutated_handler_class(mutation)))
