# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which contract key supplied a resolved Definition-of-Done band (OMN-17765)."""

from enum import StrEnum


class EnumDodBandSource(StrEnum):
    """The contract key a resolved DoD band was read from.

    Recorded on the routing decision so a band can be read back rather than
    inferred. Before this existed, the shape was resolved and then discarded and
    only its *effect* reached the payload -- so the 28/24 split on OMN-17765 had
    to be inferred from which heuristic band appeared, and OMN-17879 has to
    settle its own 24 rows by comparing timestamps against a deploy boundary
    instead of reading a field.

    The same reasoning as the ``skipped`` list in
    ``handler_quality_gate._evaluate_deterministic_checks`` (OMN-13850): a band
    that was overridden and one that was never overridden are different facts,
    and reporting only the resulting tuple makes them indistinguishable.
    """

    CLASS_DEFINITION_OF_DONE = "class_definition_of_done"
    """The task class's own ``definition_of_done`` band -- no override applied."""

    CLASS_SHAPE_OVERRIDES = "class_shape_overrides"
    """``definition_of_done.shape_overrides.<shape>`` on the task class."""

    DEFAULT_SHAPE_OVERRIDES = "default_shape_overrides"
    """The contract-wide ``default_shape_overrides.<shape>`` fallback.

    Heuristic band only. The deterministic band never resolves from here -- that
    key applies to every task class at once, so a deterministic sibling there
    would be a caller-controlled bypass of the deterministic proof surface
    (OMN-17765 C1).
    """
