# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18332. The pin definition has not drifted from the consumer's.

``omnimarket.occ_contract_pin`` holds bytes copied out of
`onex_change_control`'s ``ac_criteria`` module. It is the third copy of one
definition -- OCC owns it, `omnibase_infra`'s evidence closer ports the hash
half for the same dependency reason, and this module ports the reader half so
the producer can pin what the consumer will recompute. Three copies of one
definition drift, and the drift is silent: the digests simply stop matching and
every binding reads as a criterion that was rewritten.

Three legs, proving different things:

* :class:`TestPortedSpansMatchThePinnedSnapshot` runs everywhere, hosted CI
  included. It compares this module against a COMMITTED snapshot of the
  upstream text and catches an edit made here.
* :class:`TestTheDigestIsTheChangeControlDigest` runs everywhere too. It pins
  the same digest vectors `omnibase_infra`'s own port pins in
  ``test_omn_18330_criterion_hash.py``, so a change to any one of the three
  copies fails with a test naming the others -- which is the cross-repo fixture
  this defect was missing.
* :class:`TestThePinAgreesWithTheLiveConsumer` runs only where an
  `onex_change_control` clone is reachable. It re-derives the snapshot from the
  pinned revision AND recomputes a real corpus through the REAL consumer
  module, so a snapshot committed wrong, or a pin bumped without re-extracting,
  is caught on the lab hosts and on any developer machine.

**The honest bound**, the same one the sibling vendor test records: hosted CI
has the clone-free legs only. They prove this module matches the snapshot and
reproduces the shared vectors, not that the snapshot still matches upstream.
Bumping :data:`omnimarket.occ_contract_pin.PORTED_FROM_REVISION` is the
deliberate edit that re-extracts, and that is the control.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket import occ_contract_pin
from omnimarket.occ_contract_pin import (
    MAX_CRITERION_HASH_INPUT_CHARS,
    PORTED_FROM_PATH,
    PORTED_FROM_REVISION,
    contract_pin_hashes,
    criterion_hash,
    normalise_criterion,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "occ"
_SNAPSHOT = _FIXTURES / "omn_18332_contract_pin_ported_span.txt"
_CORPUS = _FIXTURES / "omn_18358_live_description.md"

_MODULE_SOURCE = Path(occ_contract_pin.__file__).read_text(encoding="utf-8")


def _snapshot_lines() -> list[str]:
    """The snapshot's non-blank lines, which is the unit compared.

    Line-wise rather than whole-file because the port interleaves the two
    upstream spans with its own section markers; every upstream LINE must still
    be present verbatim, in order, and a line-wise comparison says which one
    went missing instead of printing two files.
    """
    return [line for line in _SNAPSHOT.read_text(encoding="utf-8").splitlines() if line]


class TestPortedSpansMatchThePinnedSnapshot:
    """The clone-free leg. Runs in hosted CI."""

    def test_every_pinned_line_is_present_byte_for_byte(self) -> None:
        module_lines = set(_MODULE_SOURCE.splitlines())
        missing = [line for line in _snapshot_lines() if line not in module_lines]

        assert not missing, (
            "the ported module no longer contains these upstream lines "
            "verbatim; re-extract from the pinned revision rather than editing "
            f"the copy: {missing[:5]}"
        )

    def test_the_snapshot_pins_every_definition_the_pin_calls(self) -> None:
        """A positive control on the comparison above.

        Without it a snapshot that had lost its content would pass the first
        test trivially -- zero lines are all present.
        """
        snapshot = _SNAPSHOT.read_text(encoding="utf-8")

        for name in (
            "def criteria_by_label",
            "def criterion_hash",
            "def normalise_criterion",
            "def canonical_ac_label",
            "def acceptance_criteria_items",
            "def item_text",
            "def is_ac_heading",
            "MAX_CRITERION_HASH_INPUT_CHARS = 4000",
            "_AC_LABEL_RE = re.compile",
            "_LIST_ITEM_RE = re.compile",
        ):
            assert name in snapshot, f"{name} is not pinned by the snapshot"

    def test_the_ported_module_adds_no_parsing_logic_of_its_own(self) -> None:
        """Everything outside the ported spans must be inert.

        The one function below the markers is :func:`contract_pin_hashes`,
        which composes two ported functions and parses nothing. A second
        definition there would be a fourth parser wearing the port's name.
        """
        _, _, tail = _MODULE_SOURCE.partition("# PORTED SPANS END")
        defined = re.findall(r"^def (\w+)", tail, flags=re.MULTILINE)

        assert defined == ["contract_pin_hashes"]
        assert not re.search(r"^class ", tail, flags=re.MULTILINE)


class TestTheDigestIsTheChangeControlDigest:
    """The shared vectors. A drift in ANY of the three copies fails here.

    These are `onex_change_control`'s own vectors, duplicated here exactly as
    `omnibase_infra` duplicates them in
    ``tests/unit/nodes/node_evidence_autoclose_sweep_effect/test_omn_18330_criterion_hash.py``.
    The duplication IS the coupling; this class is what makes it fail loudly
    instead of drifting quietly.
    """

    def test_the_digest_is_sha256_of_the_normalised_text(self) -> None:
        assert (
            criterion_hash("AC1: anything")
            == hashlib.sha256(b"AC1: anything").hexdigest()
        )

    def test_rewrapping_a_paragraph_is_not_a_rewrite(self) -> None:
        wrapped = "AC1: the lane is\ngreen and stays\n  green."
        flowed = "AC1: the lane is green and stays green."

        assert criterion_hash(wrapped) == criterion_hash(flowed)

    def test_a_negation_is_a_rewrite(self) -> None:
        assert criterion_hash("AC1: the gate refuses it.") != criterion_hash(
            "AC1: the gate does not refuse it."
        )

    def test_a_case_change_is_a_rewrite(self) -> None:
        assert criterion_hash("AC1: the lane is green.") != criterion_hash(
            "AC1: the lane MUST be green."
        )

    def test_the_digest_is_a_full_lowercase_sha256(self) -> None:
        value = criterion_hash("AC1: anything")

        assert len(value) == 64
        assert value == value.lower()

    def test_the_input_ceiling_matches_change_control(self) -> None:
        """4000 chars, truncated deterministically -- OCC's own ceiling."""
        assert MAX_CRITERION_HASH_INPUT_CHARS == 4000
        assert len(normalise_criterion("x" * 5000)) == 4000

    def test_markdown_emphasis_is_not_normalised_away(self) -> None:
        """The vector this defect needed and nobody had.

        The producer's comparison projection strips inline markup; the
        consumer's normaliser does not touch it. Pinning the stripped form is
        what made all six entries of OCC#9486 read as stale, so the difference
        is pinned as a vector rather than left as a fact somebody has to know.
        """
        assert criterion_hash("**AC1** the lane is green") != criterion_hash(
            "AC1 the lane is green"
        )
        assert criterion_hash("AC1 run `pytest`") != criterion_hash("AC1 run pytest")

    def test_unicode_is_not_renormalised(self) -> None:
        """The producer's own vendored hash applies NFC; this one must not.

        Both spellings below are the same character sequence to NFC and
        different byte sequences to sha256. A port that quietly added NFC would
        agree with the guard and disagree with the gate.
        """
        composed = "AC1: café is green"
        decomposed = "AC1: café is green"

        assert criterion_hash(composed) != criterion_hash(decomposed)


def _change_control_root() -> Path | None:
    """The `onex_change_control` clone, or ``None`` where there is not one.

    Resolved from ``OMNI_HOME`` with no default path: a hardcoded fallback
    would silently pass this leg against the wrong tree on a machine that has
    one.
    """
    omni_home = os.environ.get("OMNI_HOME")
    if not omni_home:
        return None
    candidate = Path(omni_home) / "onex_change_control"
    return candidate if (candidate / ".git").exists() else None


def _upstream_source(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "show", f"{PORTED_FROM_REVISION}:{PORTED_FROM_PATH}"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return None
    return result.stdout if result.returncode == 0 else None


class TestThePinAgreesWithTheLiveConsumer:
    """The clone leg. Runs on the lab hosts and developer machines."""

    def test_the_snapshot_is_still_what_the_pinned_revision_says(self) -> None:
        root = _change_control_root()
        if root is None:
            pytest.skip(
                "no onex_change_control clone under $OMNI_HOME; the clone-free "
                "legs above still ran and are what hosted CI enforces"
            )
        upstream = _upstream_source(root)
        if upstream is None:
            pytest.skip(
                "the pinned onex_change_control revision is not present in this "
                "clone (fetch it to run this leg)"
            )

        missing = [line for line in _snapshot_lines() if line not in upstream]

        assert not missing, (
            "the committed snapshot is not what the pinned revision contains; "
            f"re-extract it rather than editing either side: {missing[:5]}"
        )

    def test_the_real_consumer_module_computes_the_same_digests(self) -> None:
        """The end-to-end control: load OCC's module and compare digests.

        Line-presence proves the bytes were copied. This proves they still MEAN
        the same thing, over a real ticket body, through the consumer's own
        import graph rather than through this repository's copy of it.
        """
        root = _change_control_root()
        if root is None:
            pytest.skip("no onex_change_control clone under $OMNI_HOME")
        module_path = root / PORTED_FROM_PATH
        if not module_path.exists():
            pytest.skip(f"{PORTED_FROM_PATH} is absent from the clone")

        spec = importlib.util.spec_from_file_location(
            "_omn18332_live_occ_ac_criteria", module_path
        )
        assert spec is not None
        assert spec.loader is not None
        upstream_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = upstream_module
        try:
            spec.loader.exec_module(upstream_module)
            body = _CORPUS.read_text(encoding="utf-8")
            theirs = {
                label: upstream_module.criterion_hash(text)
                for label, text in upstream_module.criteria_by_label(body).items()
            }
        finally:
            sys.modules.pop(spec.name, None)

        assert theirs, "positive control: the corpus must resolve some criteria"
        assert contract_pin_hashes(body) == theirs
