# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""No file in this public repository declares itself restricted and then
inlines the authoritative text it is restricting (OMN-18026, epic OMN-17992).

``adr_canary_ground_truth_manifest.v1.yaml`` was 611,717 bytes of exactly that:
a header declaring ``source_visibility: "private"`` /
``publication_classification: "restricted"`` / ``kb_destination: "private"``
about itself, followed by 60 entries each inlining the full text of an
authoritative ADR. It shipped in a public tree for four months.

These tests are the standing form of that finding. Each one fails on the commit
before the fix, which is how it is known not to be vacuous:

* the two file-presence tests fail because both files are there;
* :func:`test_no_config_declares_itself_restricted_and_inlines_its_source`
  fails naming the manifest;
* :func:`test_the_manifest_path_is_not_a_silent_default` fails because
  ``ModelCanaryCommandPayload()`` constructs with the in-repo path;
* the two waiver tests fail because both waivers still name the file.

The last three matter more than the deletions. Deleting a file is a one-time
act; a default that resolves to an in-repo path, and a pair of waivers that
pre-excuse that path from the repository's own leak scan, are what would let
the next copy land unnoticed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_REMOVED_PATHS = [
    "src/omnimarket/configs/adr_canary_ground_truth_manifest.v1.yaml",
    "docs/adr-canary/source_corpus_manifest.json",
]

_RESTRICTED_DECLARATION = 'publication_classification: "restricted"'
_INLINED_AUTHORITATIVE_TEXT = "ground_truth_adr: |"


@pytest.mark.unit
@pytest.mark.parametrize("relative_path", _REMOVED_PATHS)
def test_the_restricted_corpus_is_not_in_the_public_tree(relative_path: str) -> None:
    """The corpus and its source index are gone from this repository.

    They are not lost: both are archived byte-identical in the private
    knowledge base under ``reports/evidence-archive/omnimarket/``, in a pull
    request that merged before the one deleting them here.
    """
    assert not (_REPO_ROOT / relative_path).exists(), (
        f"{relative_path} is back in a public repository. It is archived in the "
        "private knowledge base; a copy here republishes it."
    )


@pytest.mark.unit
def test_no_config_declares_itself_restricted_and_inlines_its_source() -> None:
    """A config may be restricted, or may inline ADR text. Never both, here.

    The discovery manifest is deliberately still restricted-classified: it
    carries source paths and rationale prose, not the authoritative text of the
    decisions themselves, so it fails the first half of this conjunction only.
    The combination is the thing that must not exist in a public tree.
    """
    offenders = []
    for path in sorted((_REPO_ROOT / "src" / "omnimarket" / "configs").rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        if _RESTRICTED_DECLARATION in text and _INLINED_AUTHORITATIVE_TEXT in text:
            offenders.append(path.relative_to(_REPO_ROOT).as_posix())

    assert offenders == [], (
        "these configs declare themselves restricted AND inline the authoritative "
        f"text they restrict: {offenders}"
    )


@pytest.mark.unit
def test_the_manifest_path_is_not_a_silent_default() -> None:
    """``manifest_path`` is required, so an unset value refuses.

    A default pointing at an in-repo path is what re-publishes the corpus the
    day someone re-adds the file: the node would read it without anyone
    choosing to. Required-with-no-default makes that a refusal instead
    (CLAUDE.md rule 8).
    """
    from pydantic import ValidationError

    from omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_request import (
        ModelCanaryCommandPayload,
    )

    field = ModelCanaryCommandPayload.model_fields["manifest_path"]
    assert field.is_required(), (
        "manifest_path carries a default again; a default here is a silent "
        "re-publish of whatever in-repo path it names"
    )

    with pytest.raises(ValidationError):
        ModelCanaryCommandPayload()  # type: ignore[call-arg]


@pytest.mark.unit
@pytest.mark.parametrize(
    "waiver_path",
    ["scripts/validation/check_leaked_literals.sh", ".pre-commit-config.yaml"],
)
def test_no_waiver_still_pre_excuses_the_removed_corpus(waiver_path: str) -> None:
    """The two waivers died with the file they excused.

    A waiver that outlives its subject is worse than no waiver: it silently
    pre-approves the next file that lands at that path, and the scan that would
    have caught it reports green.
    """
    text = (_REPO_ROOT / waiver_path).read_text(encoding="utf-8")
    assert "adr_canary_ground_truth_manifest" not in text, (
        f"{waiver_path} still waives the removed corpus by path"
    )
