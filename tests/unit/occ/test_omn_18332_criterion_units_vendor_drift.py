# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18332. The vendored criterion parser has not drifted from the guard.

``omnimarket.occ_criterion_units`` holds bytes copied out of omniclaude's
ticket-creation admission guard. A binding's ``criterion_hash`` only means
anything if the hash the transcriber pins is the hash the GUARD computed when it
admitted the create -- otherwise "was this criterion rewritten" is answered by
two parsers that disagree about where one criterion ends, and a criterion binds
to a check that was declared for its neighbour.

Two legs, and they prove different things:

* :class:`TestVendoredSpansMatchThePinnedSnapshot` runs everywhere, including
  hosted CI. It compares the vendored module against a COMMITTED snapshot of the
  upstream text and catches an edit made here.
* :class:`TestPinnedSnapshotMatchesUpstream` runs only where an ``omniclaude``
  clone is reachable -- the lab hosts and developer machines. It re-derives the
  snapshot from the pinned revision and catches a snapshot that was committed
  wrong, or a pin bumped without re-extracting.

**The honest bound:** neither leg can detect that the pinned revision has been
superseded by a newer upstream change. Nothing in this repo can, because the pin
is the statement of which upstream bytes are intended. Bumping it is a
deliberate edit that re-extracts the snapshot, and that is the control.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from omnimarket import occ_criterion_units
from omnimarket.occ_criterion_units import (
    DEFAULT_CRITERION_POLICY,
    VENDORED_FROM_PATH,
    VENDORED_FROM_REVISION,
)

pytestmark = pytest.mark.unit

_SNAPSHOT = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "occ"
    / "omn_18332_criterion_units_pinned_span.txt"
)

_MODULE_SOURCE = Path(occ_criterion_units.__file__).read_text(encoding="utf-8")

#: The upstream code takes the guard's whole ``Policy``; the vendored copy takes
#: the two-field :class:`CriterionPolicy` that satisfies every use. This is the
#: ONLY edit the vendoring makes, and naming it here is what stops the drift
#: comparison from being quietly widened to tolerate a second one.
_PERMITTED_SUBSTITUTION = ("policy: Policy", "policy: CriterionPolicy")

#: Separates one pinned definition from the next in the snapshot file. The
#: definitions contain blank lines of their own, so a blank line is not a usable
#: boundary and an explicit marker is.
_SNAPSHOT_DELIMITER = "\n# ---- next vendored definition ----\n"


def _spans(snapshot_text: str) -> list[str]:
    """The snapshot split into the individual definitions it pins."""
    return [
        block.strip("\n")
        for block in snapshot_text.split(_SNAPSHOT_DELIMITER)
        if block.strip()
    ]


class TestVendoredSpansMatchThePinnedSnapshot:
    """The clone-free leg. Runs in hosted CI."""

    def test_every_pinned_span_is_present_byte_for_byte(self) -> None:
        missing = [
            span
            for span in _spans(_SNAPSHOT.read_text(encoding="utf-8"))
            if span.replace(*_PERMITTED_SUBSTITUTION) not in _MODULE_SOURCE
        ]

        assert not missing, (
            "the vendored module no longer contains these upstream definitions "
            "verbatim; re-extract from the pinned revision rather than editing "
            f"the copy: {[span.splitlines()[0] for span in missing]}"
        )

    def test_the_snapshot_pins_every_definition_the_transcriber_calls(self) -> None:
        """A positive control on the comparison above.

        Without it a snapshot that had lost a span would pass the first test
        trivially -- zero spans are all present.
        """
        snapshot = _SNAPSHOT.read_text(encoding="utf-8")

        for name in (
            "def criterion_units",
            "def canonical_criterion_text",
            "def _acceptance_criteria_items",
            "def _falsifier_of",
            "def _is_criteria_heading",
            "class CriterionUnit",
            "_CRITERION_LABEL: Final",
            "_LIST_ITEM: Final",
            "_AC_ITEM: Final",
        ):
            assert name in snapshot, f"{name} is not pinned by the snapshot"

    def test_the_vendored_module_adds_no_parsing_logic_of_its_own(self) -> None:
        """Everything outside the pinned spans must be inert.

        A helper added below the span markers would be a second parser wearing
        the vendored module's name, which is the exact failure the vendoring
        exists to prevent.
        """
        _, _, tail = _MODULE_SOURCE.partition("# VENDORED SPANS END")

        assert not re.search(r"^def |^class ", tail, flags=re.MULTILINE), (
            "a definition was added after the vendored spans; parsing logic "
            "belongs upstream, in the guard, not here"
        )

    def test_the_vendored_policy_vocabulary_matches_the_guards(self) -> None:
        """The vocabulary is pinned for the same reason the code is.

        A falsifier marker added upstream but missing here stops reading a
        falsifier the guard admitted, which silently demotes a properly
        declared criterion to a draft.
        """
        assert DEFAULT_CRITERION_POLICY.falsifier_markers == (
            "falsifier:",
            "falsified by",
        )
        assert (
            "acceptance criteria"
            in DEFAULT_CRITERION_POLICY.acceptance_criteria_headings
        )
        assert (
            "definition of done"
            in DEFAULT_CRITERION_POLICY.acceptance_criteria_headings
        )


def _omniclaude_root() -> Path | None:
    """The omniclaude clone, or ``None`` where there is not one.

    Resolved from ``OMNI_HOME`` with no default path: a hardcoded fallback would
    silently pass this leg against the wrong tree on a machine that has one.
    """
    omni_home = os.environ.get("OMNI_HOME")
    if not omni_home:
        return None
    candidate = Path(omni_home) / "omniclaude"
    return candidate if (candidate / ".git").exists() else None


class TestPinnedSnapshotMatchesUpstream:
    """The clone leg. Runs on the lab hosts and developer machines."""

    def test_the_snapshot_is_still_what_the_pinned_revision_says(self) -> None:
        root = _omniclaude_root()
        if root is None:
            pytest.skip(
                "no omniclaude clone under $OMNI_HOME; the clone-free leg above "
                "still ran and is what hosted CI enforces"
            )
        try:
            upstream = subprocess.run(
                ["git", "show", f"{VENDORED_FROM_REVISION}:{VENDORED_FROM_PATH}"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover
            pytest.skip(f"git is not usable in this environment: {error}")
        if upstream.returncode != 0:
            pytest.skip(
                "the pinned omniclaude revision is not present in this clone "
                f"(fetch it to run this leg): {upstream.stderr.strip()[:200]}"
            )

        missing = [
            span
            for span in _spans(_SNAPSHOT.read_text(encoding="utf-8"))
            if span not in upstream.stdout
        ]

        assert not missing, (
            "the committed snapshot is not what the pinned omniclaude revision "
            "contains; re-extract it rather than editing either side: "
            f"{[span.splitlines()[0] for span in missing]}"
        )
