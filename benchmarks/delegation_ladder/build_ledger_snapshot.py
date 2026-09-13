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

What survives redaction is what the benchmark is about: ticket identifiers,
repository and pull-request references, lane names, verdicts and prose. The R2
scorers grade identifier grounding and coverage, and none of the redacted
classes is an identifier a summary should be repeating.

Run: ``python benchmarks/delegation_ladder/build_ledger_snapshot.py <ledger path>``
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SNAPSHOT = FIXTURES / "ledger_rows_snapshot.txt"

ROW_RE = re.compile(r"^\|?\s*2026-09-1[123]T")

# Each entry is (pattern, replacement). The classes mirror the repository's
# leaked-literals gate; the replacements are readable placeholders rather than a
# blanket mask, so a redacted row still reads as a sentence.
STRUCTURAL: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/Users/[A-Za-z0-9_.-]+/Code/omni_home"), "$OMNI_HOME"),
    (re.compile(r"/Users/[A-Za-z0-9_.-]+"), "$HOME"),
    (re.compile(r"/home/[A-Za-z0-9_.-]+/"), "$HOME/"),
    (re.compile(r"/Volumes/[A-Za-z0-9_-]+"), "$VOLUME"),
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
        "# instance ids, operator attribution and personal handles. Ticket identifiers,",
        "# repository references, lane names and prose are untouched, because those are",
        "# what the R2 scorers grade.",
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
