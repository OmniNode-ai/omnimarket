#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18374 -- a vendor's name may appear only inside an effect node's ``adapters/``.

THE RULE
--------
Operator ruling, 2026-09-14 (in-session): "We should not have any
Repowise-specific fields in anything except for adapters."

A vendor is something the platform talks to, and the architecture already names
the one place that talking happens: an EFFECT node's ``adapters/`` package,
behind a protocol the rest of the system depends on instead. Everywhere else in
``src/omnimarket/`` -- a contract value, a model field, a handler branch, a node
package NAME -- a vendor token means the vendor's name has become part of the
platform's own vocabulary. That is what turns swapping the vendor from "write
one new adapter" into a repo-wide rewrite, and it is what this gate refuses.

WHAT IS SCANNED
---------------
Every text file under ``src/omnimarket/``, matched CASE-INSENSITIVELY as a plain
substring against both the file's CONTENT and its repo-relative PATH. Content
and path both, because ``node_kb_repowise_index_effect/`` leaks the vendor in
the package name alone, and a path-blind scan would call a directory named after
a vendor clean. Substring rather than word-boundary, because ``AdapterRepoWiseCLI``,
``repowise_index``, ``REPOWISE_API_KEY`` and ``repowise-index.v1`` are the same
leak wearing four spellings. Python identifiers, string literals and
``contract.yaml`` values are therefore all in scope with no per-language parser.

A hit is ALLOWED, and never recorded, when the path is inside
``src/omnimarket/nodes/<node>/adapters/``. That is the whole exemption. There is
no inline annotation and no per-file opt-out: a comment cannot make a vendor name
stop being a vendor name.

THE DENY-LIST
-------------
``config/validation/vendor_tokens.yaml``. Adding a vendor is a one-line change
there; it takes effect on the next run.

RATCHET, NOT A GREEN-FIELD ASSERTION
------------------------------------
``repowise`` is live in this tree today outside ``adapters/`` -- knowledge query
federation, post-merge knowledge sync, the knowledge health probe and compute,
platform diagnostics, recall, design-plan context, the dispatch worker, and the
whole ``node_kb_repowise_index_effect`` package. A hard failure would block every
commit in the repo, so the baseline records today's violations and this gate
fails only when they GROW: a file not in the baseline, or a baselined file whose
occurrence count went UP. A count that goes DOWN is the ratchet working and is
never a failure; it prints the rows that can be dropped.

Regenerate after legitimately removing (or moving into ``adapters/``) a vendor
token::

    uv run python scripts/validation/check_vendor_token_scope.py --write-baseline

The baseline may only shrink. A regeneration that adds rows, or raises a count,
is REFUSED unless ``--allow-growth`` is passed, so "fix the baseline" cannot
quietly become the way a new leak lands. The baseline is never hand-edited.

Same shape as ``check_projection_cursor_declared.py`` (OMN-18043) and its
``projection_cursor_baseline.txt`` next to it -- deliberately, so there is one
ratchet mechanism in this repo rather than two.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOT = REPO_ROOT / "src" / "omnimarket"
DENYLIST = REPO_ROOT / "config" / "validation" / "vendor_tokens.yaml"
BASELINE = Path(__file__).resolve().parent / "vendor_token_scope_baseline.txt"

#: The one sanctioned home. Relative to the repo root, forward slashes.
ADAPTERS_PACKAGE = re.compile(r"^src/omnimarket/nodes/[^/]+/adapters/")

#: Directories that hold build products, not source.
SKIP_DIRS = frozenset({"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"})

#: Suffixes whose bytes are not text; a substring scan over them is noise.
BINARY_SUFFIXES = frozenset(
    {".pyc", ".pyo", ".so", ".dylib", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf"}
)

#: A scan finding far fewer files than the tree has is a broken scan, not a
#: clean tree (rule 16: prove a zero with a positive control).
DEFAULT_MIN_EXPECTED_FILES = 2000

_HEADER = (
    "# OMN-18374 -- vendor tokens occurring outside an effect node's adapters/ package.\n"
    "#\n"
    "# GENERATED FILE. Never hand-edited. This list may only SHRINK. Regenerate with:\n"
    "#   uv run python scripts/validation/check_vendor_token_scope.py --write-baseline\n"
    "#\n"
    "# Format: <repo-relative path>::<token> <occurrence count>\n"
    "# The count includes occurrences in the path itself, so a package named after\n"
    "# a vendor is a violation even when no line inside it mentions one.\n"
)


@dataclass(frozen=True, slots=True)  # internal-dataclass-ok: validator-internal row
class VendorTokenRow:
    """One (file, vendor token) pair and how many times the token occurs."""

    path: str
    token: str
    count: int

    @property
    def key(self) -> str:
        return f"{self.path}::{self.token}"

    def render(self) -> str:
        return f"{self.key} {self.count}"


def load_tokens(denylist: Path) -> tuple[str, ...]:
    """Every deny-listed vendor token, lowercased and sorted.

    Fails closed: a missing, unparseable, or empty deny-list raises rather than
    scanning for nothing and reporting a clean tree.
    """
    if not denylist.exists():
        raise FileNotFoundError(f"vendor-token deny-list not found: {denylist}")
    data = yaml.safe_load(denylist.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"deny-list is not a mapping: {denylist}")
    entries = data.get("tokens")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"deny-list declares no tokens: {denylist}")
    tokens: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"deny-list entry is not a mapping: {entry!r}")
        token = entry.get("token")
        if not isinstance(token, str) or not token.strip():
            raise ValueError(f"deny-list entry declares no token: {entry!r}")
        tokens.append(token.strip().lower())
    return tuple(sorted(set(tokens)))


def _scannable(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        yield path


def scan(
    scan_root: Path, tokens: tuple[str, ...], repo_root: Path
) -> tuple[tuple[VendorTokenRow, ...], int]:
    """Every vendor-token occurrence outside an adapters/ package, plus files seen."""
    rows: list[VendorTokenRow] = []
    files = 0
    for path in _scannable(scan_root):
        files += 1
        relative = path.relative_to(repo_root).as_posix()
        if ADAPTERS_PACKAGE.match(relative):
            continue
        lowered_path = relative.lower()
        try:
            lowered_text = path.read_text(errors="replace").lower()
        except OSError:
            continue
        for token in tokens:
            count = lowered_path.count(token) + lowered_text.count(token)
            if count:
                rows.append(VendorTokenRow(path=relative, token=token, count=count))
    return tuple(sorted(rows, key=lambda row: row.key)), files


def read_baseline(baseline: Path) -> dict[str, int]:
    """The recorded violations, keyed ``<path>::<token>``."""
    if not baseline.exists():
        return {}
    recorded: dict[str, int] = {}
    for line in baseline.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, count = stripped.rpartition(" ")
        if not key:
            raise ValueError(f"malformed baseline row (no count): {stripped!r}")
        recorded[key] = int(count)
    return recorded


def write_baseline(baseline: Path, rows: tuple[VendorTokenRow, ...]) -> None:
    baseline.write_text(_HEADER + "".join(f"{row.render()}\n" for row in rows))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="regenerate the baseline from the current tree",
    )
    parser.add_argument(
        "--allow-growth",
        action="store_true",
        help="permit --write-baseline to record MORE violations than before",
    )
    parser.add_argument(
        "--scan-root",
        type=Path,
        default=SCAN_ROOT,
        help="tree to scan (default: src/omnimarket)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="root that reported paths are relative to",
    )
    parser.add_argument(
        "--denylist", type=Path, default=DENYLIST, help="vendor-token deny-list"
    )
    parser.add_argument("--baseline", type=Path, default=BASELINE, help="baseline file")
    parser.add_argument(
        "--min-files",
        type=int,
        default=DEFAULT_MIN_EXPECTED_FILES,
        help="vacuity floor: fail if fewer files than this were scanned",
    )
    args = parser.parse_args(argv)

    tokens = load_tokens(args.denylist)
    rows, files = scan(args.scan_root, tokens, args.repo_root)

    if files < args.min_files:
        print(
            f"[vendor-token-scope] FAIL (vacuity guard): only {files} file(s) scanned "
            f"under {args.scan_root} (expected >= {args.min_files}). A gate over a "
            "collapsed set proves nothing.",
            file=sys.stderr,
        )
        return 1

    baseline = read_baseline(args.baseline)
    current = {row.key: row.count for row in rows}

    if args.write_baseline:
        added = sorted(key for key in current if key not in baseline)
        grown = sorted(
            f"{key} {baseline[key]} -> {count}"
            for key, count in current.items()
            if key in baseline and count > baseline[key]
        )
        bootstrapping = not args.baseline.exists()
        if (added or grown) and not args.allow_growth and not bootstrapping:
            print(
                "REFUSING to write a baseline that grows. These are NEW vendor-token "
                "leaks, not pre-existing ones:\n"
                + "".join(f"  + {item}\n" for item in added)
                + "".join(f"  ^ {item}\n" for item in grown)
                + "Move the vendor name into the node's adapters/ package instead, or "
                "pass --allow-growth with a reason.",
                file=sys.stderr,
            )
            return 1
        write_baseline(args.baseline, rows)
        print(
            f"baseline written: {len(rows)} row(s), {sum(current.values())} occurrence(s)"
        )
        return 0

    new = sorted(key for key in current if key not in baseline)
    grown = sorted(
        f"{key}: {baseline[key]} -> {count}"
        for key, count in current.items()
        if key in baseline and count > baseline[key]
    )
    shrunk = sorted(
        f"{key}: {baseline[key]} -> {current.get(key, 0)}"
        for key in baseline
        if current.get(key, 0) < baseline[key]
    )

    if new or grown:
        print(
            f"[vendor-token-scope] FAIL: {len(new)} new file(s) and {len(grown)} "
            "baselined file(s) carry a vendor token outside an effect node's "
            "adapters/ package (OMN-18374):\n"
            + "".join(f"  + {item}\n" for item in new)
            + "".join(f"  ^ {item}\n" for item in grown)
            + "\nA vendor's name belongs behind a protocol in "
            "src/omnimarket/nodes/<node>/adapters/ and nowhere else in src/. Name the "
            "CAPABILITY in contracts, models, handlers and package names; name the "
            "VENDOR only in the adapter that speaks to it.",
            file=sys.stderr,
        )
        return 1

    if shrunk:
        print(
            f"{len(shrunk)} baselined row(s) shrank -- run --write-baseline to tighten "
            "the ratchet:\n" + "".join(f"  - {item}\n" for item in shrunk)
        )

    print(
        f"[vendor-token-scope] OK: {files} file(s) scanned, {len(tokens)} token(s), "
        f"{len(rows)} baselined row(s), 0 new."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
