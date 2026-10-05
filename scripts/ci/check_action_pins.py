#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitHub Actions pin check (OMN-14762 / F-19-A, baseline retired by OMN-20558).

Third-party actions referenced by a floating tag (``@v4``, ``@main``, ``@0.34.0``)
are a supply-chain and reproducibility risk and are a recurring CodeRabbit
finding on this repo's workflows (``omnimarket#1794``). This gate requires every
``uses:`` reference to be pinned to a full 40-hex-character commit SHA.

It began as a ratchet with a frozen baseline of unpinned refs. OMN-20558 pinned
every one of them to the commit its tag resolved to and deleted the baseline, so
this is now a plain check: ANY unpinned, non-exempt ``uses:`` fails. Local ``./``
composite/reusable actions and first-party ``OmniNode-ai/...@main`` reusable
workflows are exempt (no third-party ref to pin). There is no baseline and no
flag to write one.

SYNC with the pre-commit hook ``check-action-pins`` and the ci.yml step of the
same name.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# A pinned ref is exactly 40 hex chars (optionally followed by whitespace/comment).
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
# ``uses: <action>@<ref>`` — capture the action ref, tolerate surrounding quotes
# and a trailing ``# comment``.
_USES_RE = re.compile(r"""^\s*(?:-\s*)?uses:\s*["']?([^"'#\s]+)["']?""")


def _parse_ref(uses_value: str) -> tuple[str, str | None]:
    """Split ``owner/repo/path@ref`` into (action, ref). ref is None if absent."""
    if "@" not in uses_value:
        return uses_value, None
    action, _, ref = uses_value.rpartition("@")
    return action, ref


# First-party org whose reusable workflows / composite actions are referenced by
# ``@main`` as current policy (e.g. deploy-gate-reusable.yml@main). These are
# exempt from SHA-pinning; third-party actions are not.
_FIRST_PARTY_PREFIX = "OmniNode-ai/"


def is_exempt(uses_value: str) -> bool:
    """Refs that carry no external third-party pin obligation.

    Exempt:
      * local ``./`` composite/reusable actions (no external ref), and
      * first-party ``OmniNode-ai/...@main`` reusable workflows / composite
        actions (org policy is ``@main`` for these).
    """
    if uses_value.startswith("./") or uses_value.startswith(".\\"):
        return True
    action, ref = _parse_ref(uses_value)
    if action.startswith(_FIRST_PARTY_PREFIX) and ref == "main":
        return True
    return False


def is_pinned(uses_value: str) -> bool:
    _, ref = _parse_ref(uses_value)
    return ref is not None and bool(_SHA_RE.match(ref))


def collect_uses(workflow_dir: Path) -> list[tuple[Path, int, str]]:
    """Return (file, lineno, uses_value) for every ``uses:`` in the workflow dir."""
    found: list[tuple[Path, int, str]] = []
    for wf in sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml")):
        for i, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), start=1):
            m = _USES_RE.match(line)
            if m:
                found.append((wf, i, m.group(1)))
    return found


def unpinned_refs(workflow_dir: Path) -> set[str]:
    """Distinct action refs (``action@ref``) that are unpinned and non-exempt."""
    refs: set[str] = set()
    for _, _, value in collect_uses(workflow_dir):
        if is_exempt(value) or is_pinned(value):
            continue
        refs.add(value)
    return refs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(_REPO_ROOT))
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    workflow_dir = root / ".github" / "workflows"

    if not workflow_dir.is_dir():
        print(
            f"check-action-pins: no .github/workflows dir at {workflow_dir}",
            file=sys.stderr,
        )
        return 1

    all_uses = collect_uses(workflow_dir)
    if not all_uses:
        # Non-vacuity: a repo with workflows but zero parsed `uses:` means the
        # matcher broke — fail closed rather than pass silently.
        print(
            "check-action-pins: parsed ZERO `uses:` from workflow files — matcher "
            "likely broken; failing closed.",
            file=sys.stderr,
        )
        return 1

    violations = sorted(unpinned_refs(workflow_dir))
    if violations:
        print(
            "check-action-pins: FAIL — unpinned action reference(s):",
            file=sys.stderr,
        )
        for ref in violations:
            locs = [f"{f.relative_to(root)}:{ln}" for f, ln, v in all_uses if v == ref]
            print(f"  - {ref}  ({', '.join(locs)})", file=sys.stderr)
        print(
            "  Pin each to a 40-char commit SHA (e.g. actions/checkout@<sha> # v4).",
            file=sys.stderr,
        )
        return 1

    print(
        f"check-action-pins: OK — {len(all_uses)} `uses:` line(s), all pinned or exempt."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
