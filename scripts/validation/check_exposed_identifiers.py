#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17320: exposed-identifier gate -- hash-keyed denylist, BLOCKING MODE.

Why this exists
---------------
OMN-17288 scrubbed a live tenant slug + registry UUID out of two PUBLIC repos and
established a synthetic-identifier convention in their place. Three hours later an
unrelated lane (omnimarket#2239 / OMN-16804) reintroduced the slug into two files,
and every enforced gate stayed green -- including on the very PR whose acceptance
criterion was "zero grep hits". The convention was documentation, and documentation
lost a race. Operating Rule #5: detection that is not a gate gets ignored.

Why the denylist holds digests and not the literals
---------------------------------------------------
This file and its denylist ship in PUBLIC repositories. A plaintext denylist would
create exactly the fresh, greppable, current-tree occurrence the gate exists to
prevent -- and would force the denylist to be exempt from its own rule, which is the
precise shape (a special file nobody scans, that people copy from) that produced the
incident. So the denylist stores salted SHA-256 digests plus a human-readable class
label and the owning ticket. Nothing here restates a forbidden value.

The salt is committed, so this is obfuscation, not secrecy: a reader holding this
repo can brute-force a short slug. That is stated rather than glossed, because it is
not the property being bought. The OMN-17288 values are already public in git history
and the operator ruled document-and-accept on that history. What this format buys is
FORWARD safety -- the next entry added here may be a live identifier that has not
leaked, and a plaintext denylist would be an active disclosure in that case.

Matching
--------
Each line is lowercased and split into identifier tokens; for every denylisted
length L, every L-wide window inside every token is hashed. So a literal is caught
bare (``t-example``), embedded (``tenant_t-example_v2``), and inside a path or URL
segment (``https://host/t-example/x``) -- not merely as a standalone word.

Encoded forms (base64, percent-encoding, hex-of-utf8) are deliberately NOT decoded.
OMN-17180 owns that class; claiming coverage here would be a false claim.

Exemption
---------
One escape hatch, per line, ticketed and reasoned::

    # onex-allow-exposed-identifier OMN-XXXXX reason="<concrete reason>"

A bare annotation with no ticket or no reason is REJECTED, matching every other
onex-allow class. There is deliberately NO file-level exemption for this class
(unlike the leaked-literals gate's ``# onex-allow-file``): a whole-file waiver is
how a forbidden value survives in a corner nobody reads, which is the failure this
gate was built from.

The intended use is the historical-narrative case the OMN-17288 finisher hit --
prose describing the incident itself, where substituting a synthetic stand-in would
assert a false fact about what happened. In that case the finisher dropped the
literal and kept the claim, which is still the preferred fix; the annotation exists
for when it genuinely is not.

Output
------
Findings never print the matched value -- printing it would defeat the gate. Each
finding gives ``path:line:col`` plus the match length and the denylist entry's id,
kind and owning ticket. Column + length pinpoint the token exactly for the author,
who already knows what they wrote.

Exit codes: 0 clean (or advisory), 1 findings in blocking mode, 2 usage/config error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# One denylist entry: id / kind / length / sha256 / ticket / notes. Deliberately
# NOT a TypedDict -- the loader validates the shape at runtime and must fail
# closed on a malformed file, which a static-only shape would not do.
Entry = dict[str, Any]

DEFAULT_DENYLIST = Path(__file__).with_name("exposed_identifiers_denylist.json")

# Same ticket+reason discipline as the five leaked-literals classes.
ANNOTATION_RE = re.compile(
    r"#\s*onex-allow-exposed-identifier\s+OMN-[0-9]+\s+reason=\"[^\"]+\""
)

# Identifier tokens. Lowercased input, so no A-Z class is needed.
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.\-]*")

# Directories and suffixes that are never source-of-truth for a committed literal.
EXCLUDED_DIR_PARTS = frozenset(
    {
        ".git",
        "dist",
        "build",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "htmlcov",
        ".tox",
        "site-packages",
    }
)
EXCLUDED_SUFFIXES = (
    ".lock",
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".whl",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".mp4",
    ".mov",
)

# A token longer than this is a blob (minified bundle, base64 payload), not an
# identifier a human typed. Windowing it is quadratic and finds nothing real.
MAX_TOKEN_LEN = 512

MAX_BYTES = 8 * 1024 * 1024


class DenylistError(RuntimeError):
    """The denylist is missing, malformed, or self-inconsistent."""


class Denylist:
    """Length-indexed digest lookup. Holds no plaintext, by construction."""

    def __init__(self, doc: dict[str, Any]) -> None:
        salt = doc.get("salt")
        if not isinstance(salt, str) or not salt:
            raise DenylistError("denylist has no 'salt'")
        entries = doc.get("entries")
        if not isinstance(entries, list) or not entries:
            raise DenylistError("denylist has no 'entries'")

        self.salt = salt
        # Pre-absorb the salt once; every window hash then clones this state
        # instead of re-hashing the salt bytes. The window scan is the hot loop.
        self._salted = hashlib.sha256(salt.encode("utf-8"))
        self.entries = entries
        self.by_len: dict[int, dict[str, Entry]] = {}
        for entry in entries:
            for field in ("id", "kind", "length", "sha256", "ticket"):
                if field not in entry:
                    raise DenylistError(
                        f"denylist entry missing '{field}': {entry.get('id', '?')}"
                    )
            length = entry["length"]
            if not isinstance(length, int) or length < 4:
                raise DenylistError(
                    f"entry {entry['id']}: 'length' must be an int >= 4"
                )
            digest = entry["sha256"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise DenylistError(
                    f"entry {entry['id']}: 'sha256' must be 64 lowercase hex chars"
                )
            # A plaintext field would defeat the whole design; refuse to load one.
            for forbidden in ("value", "literal", "plaintext"):
                if forbidden in entry:
                    raise DenylistError(
                        f"entry {entry['id']}: '{forbidden}' is forbidden -- this denylist "
                        f"ships in public repos and must never carry plaintext"
                    )
            self.by_len.setdefault(length, {})[digest] = entry

        self.lengths = sorted(self.by_len)
        self.min_len = self.lengths[0]

    @classmethod
    def load(cls, path: Path) -> Denylist:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DenylistError(f"denylist not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise DenylistError(f"denylist is not valid JSON: {path}: {exc}") from exc
        return cls(doc)

    def digest(self, value: str) -> str:
        hasher = self._salted.copy()
        hasher.update(value.encode("utf-8"))
        return hasher.hexdigest()

    def scan_line(self, line: str) -> list[tuple[int, int, Entry]]:
        """Return (col, length, entry) for each hit, longest-match-wins on overlap."""
        low = line.lower()
        raw: list[tuple[int, int, Entry]] = []
        for tok in TOKEN_RE.finditer(low):
            text = tok.group(0)
            span = len(text)
            if span < self.min_len or span > MAX_TOKEN_LEN:
                continue
            base = tok.start()
            for length in self.lengths:
                if span < length:
                    break  # lengths ascend; every later one is longer still
                bucket = self.by_len[length]
                for offset in range(span - length + 1):
                    entry = bucket.get(self.digest(text[offset : offset + length]))
                    if entry is not None:
                        raw.append((base + offset, length, entry))
        if len(raw) < 2:
            return raw
        # Collapse overlaps: a full UUID also matches its own prefix entry.
        raw.sort(key=lambda hit: (hit[0], -hit[1]))
        kept: list[tuple[int, int, Entry]] = []
        covered_to = -1
        for col, length, entry in raw:
            if col < covered_to:
                continue
            kept.append((col, length, entry))
            covered_to = col + length
        return kept


def iter_repo_files(root: Path, scope: str, base_ref: str) -> list[Path]:
    if scope == "diff":
        probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", base_ref],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            print(
                f"exposed-id-gate: WARN {base_ref} not found; falling back to scope=all",
                file=sys.stderr,
            )
            scope = "all"
        else:
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "diff",
                    "--name-only",
                    "-z",
                    f"{base_ref}...HEAD",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            return [root / name for name in out.split("\0") if name]
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-coz", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [root / name for name in out.split("\0") if name]


def is_scannable(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    if EXCLUDED_DIR_PARTS.intersection(rel.parts):
        return False
    if path.name.lower().endswith(EXCLUDED_SUFFIXES):
        return False
    if not path.is_file() or path.is_symlink():
        return False
    try:
        if path.stat().st_size > MAX_BYTES:
            return False
    except OSError:
        return False
    return True


def scan_file(path: Path, root: Path, denylist: Denylist) -> list[str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    if b"\0" in raw[:8192]:
        return []  # binary
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")

    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = str(path)

    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        hits = denylist.scan_line(line)
        if not hits:
            continue
        if ANNOTATION_RE.search(line):
            continue
        for col, length, entry in hits:
            findings.append(
                f"{rel}:{lineno}:{col + 1}: denylisted {entry['kind']} "
                f"(entry={entry['id']} ticket={entry['ticket']} match_len={length}) "
                f"-- value withheld by design"
            )
    return findings


def fingerprint(denylist_path: Path) -> str:
    self_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    list_digest = hashlib.sha256(denylist_path.read_bytes()).hexdigest()
    return f"scanner={self_digest} denylist={list_digest}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OMN-17320 exposed-identifier gate")
    parser.add_argument(
        "paths", nargs="*", help="explicit files to scan (pre-commit passes these)"
    )
    parser.add_argument("--mode", choices=("blocking", "advisory"), default="blocking")
    parser.add_argument(
        "--scope",
        choices=("all", "diff"),
        default="all",
        help="ignored when explicit paths are given",
    )
    parser.add_argument("--base-ref", default="origin/dev")
    parser.add_argument("--denylist", type=Path, default=DEFAULT_DENYLIST)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument(
        "--print-fingerprint",
        action="store_true",
        help="print sha256 of this scanner and its denylist, for cross-repo parity",
    )
    args = parser.parse_args(argv)

    if args.print_fingerprint:
        print(fingerprint(args.denylist))
        return 0

    try:
        denylist = Denylist.load(args.denylist)
    except DenylistError as exc:
        print(f"exposed-id-gate: ERROR {exc}", file=sys.stderr)
        return 2

    if args.root is not None:
        root = args.root.resolve()
    else:
        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        root = (
            Path(probe.stdout.strip()).resolve()
            if probe.returncode == 0
            else Path.cwd()
        )

    if args.paths:
        # Resolve against the CWD, not --root: pre-commit and CI both hand this
        # script paths relative to where they invoked it, while --root only names
        # the tree findings are reported relative to. Joining to root instead
        # silently produced a nonexistent path and a files_scanned=0 PASS -- a gate
        # that reports success because it scanned nothing is the exact failure mode
        # this ticket is about, so it is fixed rather than worked around.
        candidates = [Path(p).resolve() for p in args.paths]
        missing = [p for p in candidates if not p.exists()]
        if missing:
            print(
                "exposed-id-gate: ERROR these paths do not exist: "
                + ", ".join(str(p) for p in missing),
                file=sys.stderr,
            )
            return 2
    else:
        candidates = iter_repo_files(root, args.scope, args.base_ref)

    findings: list[str] = []
    scanned = 0
    for path in candidates:
        if not is_scannable(path, root):
            continue
        scanned += 1
        findings.extend(scan_file(path, root, denylist))

    print(
        f"exposed-id-gate: mode={args.mode} scope="
        f"{'paths' if args.paths else args.scope} entries={len(denylist.entries)} "
        f"files_scanned={scanned} findings={len(findings)}"
    )
    for finding in findings:
        print(f"  {finding}")

    if findings:
        print(
            "exposed-id-gate: a denylisted identifier is present. Replace it with a synthetic "
            "stand-in, or -- only for prose narrating the incident itself, where a stand-in "
            "would assert a false fact -- drop the literal and keep the claim. If neither "
            "applies, annotate the line:\n"
            '  # onex-allow-exposed-identifier OMN-XXXXX reason="<concrete reason>"\n'
            "Plaintext for each entry is recorded in its owning Linear ticket, not here.",
            file=sys.stderr,
        )

    if args.mode == "advisory":
        print("exposed-id-gate: advisory mode -- exit 0 regardless of findings")
        return 0
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
