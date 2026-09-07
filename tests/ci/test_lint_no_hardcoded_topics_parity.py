# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two topic-literal gates must agree (OMN-18013).

``scripts/lint_no_hardcoded_topics.py`` and ``scripts/ci/check_no_hardcoded_topics.py``
scan the same corpus in the same CI job. They used to disagree about the escape hatch:
the sibling honoured a per-line ``# onex-topic-allow: <reason>`` annotation, this one knew
only whole-file basenames, and the pre-commit ``exclude:`` regex that papered over the gap
is not consulted when CI runs the script directly over ``src/``. The same bytes were
therefore green locally and red in CI.

These tests pin the agreement itself, not one gate's verdict, so the divergence cannot
reopen silently: the markers must stay byte-identical, an annotated line must be accepted
by both, and an un-annotated one must be rejected by both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_no_hardcoded_topics import _INLINE_ALLOW_MARKERS
from scripts.ci.check_no_hardcoded_topics import scan as scan_annotated
from scripts.lint_no_hardcoded_topics import INLINE_ALLOW_MARKERS
from scripts.lint_no_hardcoded_topics import main as lint_main
from scripts.lint_no_hardcoded_topics import scan as scan_lint

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Assembled at runtime so this file does not itself trip either gate.
_TOPIC = "onex." + "evt.omnimarket.parity-probe.v1"
_TOPIC_PATTERNS_FOR_AUDIT = tuple(
    q + "onex." + kind + "." for kind in ("evt", "cmd") for q in ('"', "'")
)


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


@pytest.mark.unit
def test_both_gates_share_the_same_allow_markers() -> None:
    """One escape hatch, spelled once. Drift here is what let the gates disagree."""
    assert tuple(INLINE_ALLOW_MARKERS) == tuple(_INLINE_ALLOW_MARKERS)


@pytest.mark.unit
def test_both_gates_accept_an_annotated_literal(tmp_path: Path) -> None:
    """A per-line annotation with a reason is honoured by BOTH gates.

    This is the case that was red in CI and green locally before OMN-18013.
    """
    _write(
        tmp_path,
        "validators/fence.py",
        f'PIN = "{_TOPIC}"  # onex-topic-allow: the fence IS the pin\n',
    )
    assert scan_lint(tmp_path) == []
    assert scan_annotated(tmp_path) == []


@pytest.mark.unit
def test_both_gates_reject_the_same_unannotated_literal(tmp_path: Path) -> None:
    """Positive control: strip the annotation and BOTH gates must fail the line.

    Without this, `test_both_gates_accept_an_annotated_literal` would also pass against
    a gate that had simply stopped looking.
    """
    _write(tmp_path, "validators/fence.py", f'PIN = "{_TOPIC}"\n')
    lint_violations = scan_lint(tmp_path)
    annotated_violations = scan_annotated(tmp_path)
    assert len(lint_violations) == 1, lint_violations
    assert len(annotated_violations) == 1, annotated_violations
    assert "fence.py" in lint_violations[0]
    assert "fence.py" in annotated_violations[0]


@pytest.mark.unit
def test_the_three_validator_fences_carry_no_whole_file_allowance() -> None:
    """The fences are allowed per line, never per file.

    A whole-file allowance would let an unrelated, unreasoned literal into the same file
    unnoticed. Each of these files holds a pinned (defect, node, topic) fence and every
    one of its literals must state its own reason.
    """
    from scripts.lint_no_hardcoded_topics import ALLOWED_FILES, ALLOWED_PREFIXES

    for name in (
        "contract_topic_graph.py",
        "no_baseline_refreeze.py",
        "no_literal_event_type_in_tests.py",
    ):
        assert name not in ALLOWED_FILES, name
        assert not any(name.startswith(p) for p in ALLOWED_PREFIXES), name


@pytest.mark.unit
@pytest.mark.parametrize(
    "fence_file",
    [
        "contract_topic_graph.py",
        "no_baseline_refreeze.py",
        "no_literal_event_type_in_tests.py",
    ],
)
def test_every_literal_in_an_externally_excluded_fence_states_its_reason(
    fence_file: str,
) -> None:
    """The whole-file exclusion these files carry must never hide an unreasoned literal.

    The third-party ``no-hardcoded-topics`` pre-commit hook has no annotation support, so
    ``.pre-commit-config.yaml`` must exclude these three files wholesale — that is the
    only mechanism that hook offers. A whole-file exclusion is blunt: it would silence a
    NEW, unrelated, unreasoned topic literal added to the same file later, and nothing
    else in the stack would notice.

    So the exclusion is made safe here instead: every ONEX topic literal in these files
    must carry its own ``# onex-topic-allow: <reason>``. The repo-owned gates enforce this
    per line; this test enforces it for the file the external hook cannot see into.
    """
    path = _REPO_ROOT / "src" / "omnimarket" / "validators" / fence_file
    assert path.is_file(), path

    unreasoned = [
        f"{fence_file}:{i}: {line.strip()}"
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if any(p in line for p in _TOPIC_PATTERNS_FOR_AUDIT)
        and not line.strip().startswith("#")
        and not any(m in line for m in INLINE_ALLOW_MARKERS)
    ]
    assert unreasoned == [], (
        f"{len(unreasoned)} topic literal(s) in {fence_file} carry no "
        f"`# onex-topic-allow: <reason>`, and the file is excluded wholesale from the "
        f"external hook, so nothing else would catch them:\n" + "\n".join(unreasoned)
    )


@pytest.mark.unit
def test_lint_main_passes_on_current_repo() -> None:
    """The live tree must be clean under the fast gate, run the way CI runs it."""
    import os

    cwd = os.getcwd()
    try:
        os.chdir(_REPO_ROOT)
        assert lint_main() == 0
    finally:
        os.chdir(cwd)
