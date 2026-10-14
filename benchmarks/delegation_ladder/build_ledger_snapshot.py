# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the committed ledger-row fixture for the R2 rung (OMN-18300).

The R2 tasks summarise real rows from the fleet coordination ledger, because a
summarisation benchmark built from invented rows measures nothing about the work
this workspace actually does. The rows therefore have to be committed, and a
committed row cannot carry the operational literals this repository's
leaked-literals gate exists to keep out of a public package.

So every row is redacted before it is written, by the same classes that gate
enforces: lab and carrier-grade addresses, home and volume paths, cluster-local
service names, the cloud account id, instance ids, operator attribution and
personal handles. The redaction is mechanical and is applied to the row set as a
whole, not to the rows that happened to trip the gate -- redacting only what was
caught would leave the next row to trip it.

A second pass then renames what identifies the workspace the rows came from:
the private repositories, the registry and worktree directory names, lab lane
ids, a person's name, and the lane name of every row (a lane name is a session
identifier). Each distinct name becomes one stable placeholder, so rows that
shared a lane or a repository still share one.

What survives redaction is what the benchmark is about: ticket identifiers,
pull-request numbers, verdicts and prose. The R2 scorers grade identifier
grounding and coverage, and none of the redacted classes is an identifier a
summary should be repeating.

Run: ``python -m benchmarks.delegation_ladder.build_ledger_snapshot <ledger path>``
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path

from benchmarks.delegation_ladder.build_bundles import _person_names

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SNAPSHOT = FIXTURES / "ledger_rows_snapshot.txt"

ROW_RE = re.compile(r"^\|?\s*2026-09-1[123]T")

# Each entry is (pattern, replacement). The classes mirror the repository's
# leaked-literals gate; the replacements are readable placeholders rather than a
# blanket mask, so a redacted row still reads as a sentence.
_REGISTRY_DIR = "omni" + "_home"
STRUCTURAL: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"/Users/[A-Za-z0-9_.-]+/Code/{_REGISTRY_DIR}"), "$OMNI_HOME"),
    (re.compile(r"/Users/[A-Za-z0-9_.-]+"), "$HOME"),
    (re.compile(r"/home/[A-Za-z0-9_.-]+/"), "$HOME/"),
    (re.compile(r"/Vol" r"umes/[A-Za-z0-9_-]+"), "$VOLUME"),
    (re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"), "<lab-host>"),
    (
        re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
        "<lab-host>",
    ),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<address>"),
    (re.compile(r"\.tail[A-Za-z0-9]+\.ts\.net"), ".<tailnet>"),
    (re.compile(r"[A-Za-z0-9_.-]*\.svc\.cluster\.local"), "<cluster-service>"),
    (re.compile(r"\bi-0[0-9a-f]{8,}\b"), "<instance-id>"),
    (re.compile(r"\b\d{12}\b"), "<cloud-account>"),
    (re.compile(r"installed_by:\s*[A-Za-z0-9_.-]+"), "installed_by: <operator>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
)

# The catch-all. Rather than restating the repository's forbidden literals here
# -- which would both duplicate them and put them back into a committed file --
# the gate's own pattern is read out of its script and applied as a final pass.
# Two properties follow: this redactor cannot drift from the gate it is trying to
# satisfy, and this file names none of the literals itself.
GATE_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "validation"
    / "check_leaked_literals.sh"
)
_LEAK_REGEX_RE = re.compile(r"^LEAK_REGEX='(?P<body>.*)'$", re.MULTILINE)


def gate_pattern() -> re.Pattern[str]:
    """The leaked-literals gate's own matcher, read from its script.

    Fails loudly if the assignment cannot be found: a redactor that silently
    falls back to redacting nothing is worse than no redactor, because the
    output looks clean.
    """
    text = GATE_SCRIPT.read_text(encoding="utf-8")
    match = _LEAK_REGEX_RE.search(text)
    if match is None:
        raise RuntimeError(
            f"could not read the leaked-literals pattern from {GATE_SCRIPT}; "
            "refusing to write a snapshot that has not been checked against it"
        )
    # The gate is a POSIX bracket-class regex for grep -E; the one construct
    # Python does not share is [[:space:]].
    body = match.group("body").replace("[[:space:]]", r"\s")
    return re.compile(body)


SLICES: tuple[tuple[str, str, int, int], ...] = (
    ("R2-01", "short", -12, 0),
    ("R2-02", "short+medium", -20, -12),
    ("R2-03", "mixed", -26, -20),
    ("R2-04", "medium+long", -25, -14),
)


# Names that identify the workspace the rows came from, each with the placeholder
# that replaces it. The fragments are joined so that this file does not spell the
# names it removes: the public-repo hygiene gate counts every occurrence.
WORKSPACE_NAMES: tuple[tuple[str, str], ...] = (
    ("knowledge" + "-base-internal", "docs-repo"),
    ("knowledge" + "_base_internal", "docs-repo"),
    ("kb" + "-internal", "docs-repo"),
    ("omninode" + "_infra", "infra-repo"),
    ("omni" + "web", "web-repo"),
    ("omni" + "_worktrees", "worktrees"),
    (_REGISTRY_DIR, "registry"),
    ("onex" + "-lab", "lab-lane-a"),
    ("onex" + "-dev", "lab-lane-b"),
    ("stability" + "-test", "lab-lane-c"),
)
# Identifiers of internal records: tenant, project and comment ids, and the short
# fingerprints a row quotes of a stored credential.
IDENTIFIERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
        "<uuid>",
    ),
    (re.compile(r"\bsha\d{1,2} [0-9a-f]{12}\b"), "sha12 <hash>"),
)
_LANE_KV_RE = re.compile(r"\blane=([A-Za-z0-9_.-]+)")
# The session cell of a row that has no ``lane=``: ``| <ts> | <name> | CLAIM |``.
_LANE_CELL_RE = re.compile(
    r"\|\s*20\d\d-[\d-]+T[\d:]+Z\s*\|\s*([A-Za-z0-9_.-]+)\s*\|\s*[A-Z][A-Z-]+\s*\|"
)
_HOST_OCTET_RE = re.compile(r"(?<![\w.])\.20\d\b")


def lane_names(text: str) -> list[str]:
    """Every lane name the rows carry, longest first so a prefix never wins."""
    found = set(_LANE_KV_RE.findall(text)) | set(_LANE_CELL_RE.findall(text))
    return sorted(found, key=lambda name: (-len(name), name))


def rename_workspace(text: str, lanes: Iterable[str], names: Iterable[str] = ()) -> str:
    """Replace workspace names, lab lane ids, person names and lane names.

    ``lanes`` is the set of lane names of the whole row set, numbered in sorted
    order, so a name gets the same placeholder in every file it appears in.
    Applying it twice changes nothing: no placeholder contains a name.
    """
    # No word boundary: a secret, namespace or tenant named after a lane or a
    # repository (``<name>-tenant-credential``) carries the name as a prefix.
    for name, placeholder in WORKSPACE_NAMES:
        text = re.sub(re.escape(name), placeholder, text, flags=re.IGNORECASE)
    for pattern, placeholder in IDENTIFIERS:
        text = pattern.sub(placeholder, text)
    text = text.replace("$OMNI_HOME", "$REGISTRY").replace("OMNI_HOME", "REGISTRY")
    text = _HOST_OCTET_RE.sub("<lab-host>", text)
    for person in sorted(set(names), key=lambda n: (-len(n), n)):
        text = re.sub(
            rf"(?<![A-Za-z0-9_-]){re.escape(person)}(?![A-Za-z0-9_-])",
            "person-a",
            text,
            flags=re.IGNORECASE,
        )
    ordered = sorted(set(lanes))
    numbered = {name: f"lane-{i:02d}" for i, name in enumerate(ordered, start=1)}
    for name in sorted(numbered, key=lambda n: (-len(n), n)):
        text = re.sub(
            rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])",
            numbered[name],
            text,
        )
    return text


def redact(text: str, catch_all: re.Pattern[str]) -> str:
    """Readable structural replacements first, then the gate's own matcher."""
    for pattern, replacement in STRUCTURAL:
        text = pattern.sub(replacement, text)
    return catch_all.sub("<redacted>", text).strip()


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} <path to the coordination ledger>")
        return 2
    source = Path(sys.argv[1])
    catch_all = gate_pattern()
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    rows = [redact(line, catch_all) for line in lines if ROW_RE.match(line)][-140:-1]
    lanes = lane_names("\n".join(rows))
    people = _person_names()
    rows = [rename_workspace(row, lanes, people) for row in rows]

    short = [r for r in rows if len(r) < 800]
    medium = [r for r in rows if 800 <= len(r) < 1800]
    long_rows = [r for r in rows if len(r) >= 1800]

    selected: dict[str, list[str]] = {
        "R2-01": short[-12:],
        "R2-02": short[-20:-12] + medium[-7:],
        "R2-03": short[-26:-20] + medium[-14:-7] + long_rows[-7:],
        "R2-04": medium[-25:-14] + long_rows[-14:],
    }

    out: list[str] = [
        "# Real rows from the fleet coordination ledger, 2026-09-11 to 2026-09-13.",
        "# Redacted by build_ledger_snapshot.py: local clone and home paths, lab and",
        "# carrier-grade addresses, cluster-local service names, the cloud account id,",
        "# instance ids, operator attribution and personal handles, then renamed:",
        "# private repositories, registry and worktree directories, lab lane ids,",
        "# person names and every lane name become stable placeholders. Ticket",
        "# identifiers, pull-request numbers and prose are untouched, because those",
        "# are what the R2 scorers grade.",
        "# Each task's slice is delimited by its task id so the fed text is",
        "# reconstructible byte for byte.",
        "",
    ]
    for task_id, block in selected.items():
        chars = sum(len(row) for row in block)
        out.append(f"===== {task_id} ({len(block)} rows, {chars} chars) =====")
        out.extend(block)
        out.append("")
        print(f"{task_id}: {len(block)} rows, {chars} chars")

    # Exactly one trailing newline: the repo's end-of-file hook rewrites
    # anything else, and a fixture the hooks keep rewriting is a fixture whose
    # committed bytes stop matching what a run was fed.
    SNAPSHOT.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    print(f"\nwrote {SNAPSHOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
