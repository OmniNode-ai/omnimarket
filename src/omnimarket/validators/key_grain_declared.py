# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every projection exposure declares its key grain, and the claim is checked.

What this is for
----------------
Two projection writers publish a FIXED source coordinate and are correct to do
so. Both key on a content-addressed, immutable grain, so one source event owns
exactly one key, the serving cache only ever compares a key against a delta
derived from that same event, and discarding the repeat is intended
idempotence rather than lost data.

That exemption is real. The problem is where it is written down: in a handler
docstring. Both docstrings say, in almost the same words, that fixed
coordinates "would be wrong for a mutable key grain". **Nothing in the platform
reads that sentence.** There was no contract field carrying the grain, no
enumeration of which exposures claim it, and no gate that would refuse the next
handler to hardcode a constant on a grain that is genuinely mutable -- which is
the case that silently produces first-writer-wins for the life of a key, with
no error, no raised exception and no lag.

So the exemption becomes DECLARED rather than inferred, and this gate is what
reads the declaration.

Three refusals
--------------
1. **Undeclared grain.** An exposure with no ``key_grain`` is refused, never
   defaulted. A default is exactly how the next exposure would inherit an
   exemption nobody chose for it.
2. **A mutable-grain writer publishing a constant source coordinate.** This is
   the defect itself. Detected by walking the node's handler syntax tree, not
   by a regex: nearly every such call is multi-line, and a line-oriented scan
   under-reports it by an order of magnitude.
3. **An immutable declaration unbacked by evidence.** An exposure claiming
   ``immutable`` whose own test corpus carries no content-addressing assertion
   is refused, so the declaration cannot become a rubber stamp. The two live
   exemptions pass this because both already carry one; they were not written
   for this gate.

Two properties that make it a gate rather than a report
-------------------------------------------------------
**A vacuity floor.** A scan that enumerates far fewer exposures than the tree
holds is a broken scan, not a clean tree, and fails before any finding logic
runs. This is the shape ``no_literal_event_type_in_tests`` established and it
is copied deliberately.

**A fence, not a baseline.** A fenced row that no longer occurs is a HARD
FAILURE, so the table cannot be quietly topped up and cannot rot into an
exemption list. The fence is empty today because every exposure is declared;
the mechanism ships anyway, because a fence added under pressure later is a
baseline.

Exit codes: 0 clean, 1 on any refusal, on a stale fence row, or on the floor.

Related Tickets:
    - OMN-18908: this gate (epic OMN-18906 AC-2)
    - OMN-18013: ``no_literal_event_type_in_tests``, the ancestor whose floor
      and fence semantics this copies rather than reinvents
    - OMN-18910: the runtime dimension whose interim hardcoded exemption list
      this declaration replaces
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: Where projection node contracts live.
DEFAULT_NODES_ROOT = Path("src/omnimarket/nodes")

#: Where repository-level tests live, searched for a node's own coverage
#: alongside that node's in-package ``tests/`` directory.
DEFAULT_TESTS_ROOT = Path("tests")

#: Exposures the scan must find before a verdict means anything. Measured at
#: 65 on dev when this gate was written; the floor sits below that so that
#: honest removals do not trip it, and far above the handful a mis-rooted run
#: would enumerate. A gate over a collapsed set proves nothing, and reporting
#: green over one is the failure class epic OMN-18906 exists to close.
DEFAULT_MIN_EXPECTED_EXPOSURES = 55

#: The keyword whose constancy is the defect: the ORDERING component of the
#: source coordinate.
#:
#: ``source_partition`` is deliberately NOT in this set, and the first draft of
#: this gate was wrong to include it. The serving cache decides staleness by
#: comparing ``source_offset``; the partition selects which series the offset
#: belongs to. A writer can therefore publish a constant partition and a
#: strictly increasing offset and be entirely correct, which is exactly what
#: ``node_projection_delegation`` does: its offset is a microsecond token read
#: off the database's own write stamp, its docstring states that its key grain
#: is mutable and that the token must increase per key, and it REFUSES rather
#: than falling back to zero when the stamp is unreadable. Refusing that writer
#: would have been a gate manufacturing a defect out of a correct design, and
#: a gate that cries wolf on twelve healthy exposures gets switched off.
SOURCE_COORDINATE_KWARGS: frozenset[str] = frozenset({"source_offset"})

#: Substrings that mark a test function as asserting the content-addressing
#: premise -- that one source event owns exactly one key. Deliberately a short
#: DECLARED vocabulary rather than a general inference: a gate that tried to
#: decide semantically whether an arbitrary test proves content addressing
#: would be guessing, and a guess is what this ticket is removing.
#:
#: A matching name is not sufficient on its own. The function must also
#: contain at least one assertion, so a correctly named but empty test body
#: does not satisfy the requirement.
CONTENT_ADDRESSING_TEST_MARKERS: tuple[str, ...] = (
    "owns_its_own_key",
    "content_addressed",
    "content_addressing",
    "does_not_duplicate_a_row",
)

#: Exposures temporarily exempt from a refusal, as ``(node, topic, reason)``.
#:
#: A FENCE, not a growable baseline: every row here must still correspond to a
#: live finding, and a row that no longer occurs is a hard failure that must be
#: deleted in the same change that fixed its site. Empty is the correct state.
_FENCED: tuple[tuple[str, str, str], ...] = ()

_FENCED_PAIRS: frozenset[tuple[str, str]] = frozenset((n, t) for n, t, _ in _FENCED)


@dataclass(frozen=True)
class Exposure:
    """One projection exposure as its contract declares it."""

    node: str
    topic: str
    key_grain: str | None
    contract_path: str


@dataclass(frozen=True)
class Finding:
    """One refusal, carrying the exposure it names and why."""

    node: str
    topic: str
    code: str
    detail: str


def _exposure_sections(projection_api: object) -> list[dict[str, object]]:
    """Both contract shapes, normalised to a list of exposure mappings.

    The tree carries a single legacy exposure inline on ``projection_api`` and
    an explicit ``exposures:`` list. A gate that understood only one of them
    would report green over the other, which is the same shape of blindness
    this epic exists to remove.
    """
    if not isinstance(projection_api, dict):
        return []
    if not projection_api.get("expose"):
        return []
    raw = projection_api.get("exposures")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return [projection_api]


def collect_exposures(nodes_root: Path) -> list[Exposure]:
    """Every exposed projection exposure declared under ``nodes_root``."""
    exposures: list[Exposure] = []
    for contract in sorted(nodes_root.glob("*/contract.yaml")):
        try:
            document = yaml.safe_load(contract.read_text(errors="replace"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(document, dict):
            continue
        for section in _exposure_sections(document.get("projection_api")):
            topic = section.get("topic")
            grain = section.get("key_grain")
            exposures.append(
                Exposure(
                    node=contract.parent.name,
                    topic=str(topic) if isinstance(topic, str) else "",
                    key_grain=str(grain) if isinstance(grain, str) else None,
                    contract_path=str(contract),
                )
            )
    return exposures


def _constant_coordinate_sites(node_dir: Path) -> list[str]:
    """Source-coordinate keywords passed a literal constant, by syntax tree.

    A regex cannot do this job. Nearly every publish call in this tree spans
    several lines, and a line-oriented scan finds a small fraction of the
    sites -- measured at 2 against 24 when the sibling gate was written. So
    this walks the tree and reads the keyword's actual value node.
    """
    sites: list[str] = []
    for path in sorted(node_dir.rglob("*.py")):
        if "/tests/" in str(path) or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg not in SOURCE_COORDINATE_KWARGS:
                    continue
                if isinstance(keyword.value, ast.Constant):
                    sites.append(
                        f"{path}:{keyword.value.lineno} {keyword.arg}="
                        f"{keyword.value.value!r}"
                    )
    return sites


def _node_test_files(node: str, node_dir: Path, tests_root: Path) -> list[Path]:
    """A node's own test corpus: in-package tests plus importers of the node.

    Importer detection is by syntax tree rather than by filename convention,
    because only 13 of the projection packages carry in-package tests and a
    scan scoped to those would read as coverage while seeing a fifth of the
    tree.
    """
    files = [
        path
        for path in sorted(node_dir.rglob("*.py"))
        if path.name.startswith("test_") or "/tests/" in str(path)
    ]
    if not tests_root.exists():
        return files
    needle = f"omnimarket.nodes.{node}"
    for path in sorted(tests_root.rglob("test_*.py")):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if needle in text:
            files.append(path)
    return files


def _has_content_addressing_assertion(paths: Iterable[Path]) -> bool:
    """A test function named for the premise that also actually asserts.

    The name alone is not evidence. Requiring an assertion inside the function
    is what stops a correctly named but empty test body from satisfying a
    gate whose whole purpose is to stop a declaration becoming a rubber stamp.
    """
    for path in paths:
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not any(m in node.name for m in CONTENT_ADDRESSING_TEST_MARKERS):
                continue
            if any(isinstance(inner, ast.Assert) for inner in ast.walk(node)):
                return True
    return False


def evaluate(
    exposures: Sequence[Exposure],
    *,
    nodes_root: Path,
    tests_root: Path,
) -> list[Finding]:
    """Grade every exposure against the three refusals."""
    findings: list[Finding] = []
    constant_sites: dict[str, list[str]] = {}
    evidence: dict[str, bool] = {}

    for exposure in exposures:
        node_dir = nodes_root / exposure.node

        if exposure.key_grain is None:
            findings.append(
                Finding(
                    exposure.node,
                    exposure.topic,
                    "undeclared_key_grain",
                    "declares no key_grain. An undeclared grain is refused "
                    "rather than defaulted, because a default is how the next "
                    "exposure inherits an exemption nobody chose for it. "
                    "Declare 'immutable' only if one source event owns exactly "
                    "one key for the life of that key; otherwise 'mutable'.",
                )
            )
            continue

        if exposure.key_grain == "mutable":
            if exposure.node not in constant_sites:
                constant_sites[exposure.node] = _constant_coordinate_sites(node_dir)
            sites = constant_sites[exposure.node]
            if sites:
                findings.append(
                    Finding(
                        exposure.node,
                        exposure.topic,
                        "mutable_grain_constant_coordinate",
                        "declares key_grain: mutable while its writer passes a "
                        "literal constant source coordinate. On a mutable "
                        "grain a later event legitimately revises the row "
                        "behind an existing key, so a constant coordinate "
                        "makes every revision look staler than the first "
                        "write and produces first-writer-wins for the life of "
                        "the key. Sites: " + "; ".join(sites),
                    )
                )
            continue

        if exposure.key_grain == "immutable":
            if exposure.node not in evidence:
                evidence[exposure.node] = _has_content_addressing_assertion(
                    _node_test_files(exposure.node, node_dir, tests_root)
                )
            if not evidence[exposure.node]:
                findings.append(
                    Finding(
                        exposure.node,
                        exposure.topic,
                        "immutable_grain_unbacked",
                        "declares key_grain: immutable and its own test corpus "
                        "carries no content-addressing assertion, so the claim "
                        "rests on the declaration alone. An exemption proven "
                        "only by asserting it is a rubber stamp. Add a test "
                        "asserting that distinct source events own distinct "
                        "keys.",
                    )
                )

    return findings


def main(argv: Sequence[str] | None = None) -> int:
    """Run the gate. Returns the process exit code."""
    args = list(argv if argv is not None else sys.argv[1:])
    nodes_root = Path(args[0]) if args else DEFAULT_NODES_ROOT
    tests_root = Path(args[1]) if len(args) > 1 else DEFAULT_TESTS_ROOT
    minimum = int(args[2]) if len(args) > 2 else DEFAULT_MIN_EXPECTED_EXPOSURES

    exposures = collect_exposures(nodes_root)

    if len(exposures) < minimum:
        sys.stderr.write(
            f"[key-grain-declared] FAIL (vacuity guard): only "
            f"{len(exposures)} projection exposure(s) found under {nodes_root} "
            f"(expected >= {minimum}). A gate over a collapsed set proves "
            f"nothing, so this is read as a broken scan rather than a clean "
            f"tree.\n"
        )
        return 1

    findings = evaluate(exposures, nodes_root=nodes_root, tests_root=tests_root)
    observed = {(f.node, f.topic) for f in findings}

    stale = sorted(_FENCED_PAIRS - observed)
    if stale:
        sys.stderr.write(
            f"[key-grain-declared] FAIL (stale fence): {len(stale)} fenced "
            f"exposure(s) no longer produce a finding. A fixed site still "
            f"listed is how a fence rots into an exemption list. Delete these "
            f"entries from _FENCED:\n"
        )
        for node, topic in stale:
            sys.stderr.write(f"  - {node}  {topic}\n")
        return 1

    live = [f for f in findings if (f.node, f.topic) not in _FENCED_PAIRS]
    if live:
        sys.stderr.write(
            f"[key-grain-declared] FAIL: {len(live)} projection exposure(s) "
            f"refused, of {len(exposures)} scanned.\n"
        )
        for finding in live:
            sys.stderr.write(
                f"  - [{finding.code}] {finding.node} {finding.topic}\n"
                f"      {finding.detail}\n"
            )
        return 1

    sys.stdout.write(
        f"[key-grain-declared] OK: {len(exposures)} projection exposure(s) "
        f"scanned, every one declares a key grain, no mutable-grain writer "
        f"publishes a constant source coordinate, and every immutable "
        f"declaration is backed by a content-addressing assertion.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
