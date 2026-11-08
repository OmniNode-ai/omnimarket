# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain tests of the quarantine node, and the named mutations that turn each red (OMN-23144 AC4)."""

from __future__ import annotations

import pytest

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
