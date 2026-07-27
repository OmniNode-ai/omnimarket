#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitHub Actions pin ratchet (OMN-14762 / F-19-A).

Third-party actions referenced by a floating tag (``@v4``, ``@main``, ``@0.34.0``)
are a supply-chain and reproducibility risk and are a recurring CodeRabbit
finding on this repo's workflows (``omnimarket#1794``). This gate requires every
``uses:`` reference to be pinned to a full 40-hex-character commit SHA.

Because the existing workflows carry a large mixed set of already-tag-pinned
actions, this is a RATCHET rather than a flag-day rewrite: the currently
unpinned references are frozen in ``action_pin_baseline.txt`` (burn-down only),
and any NEW unpinned reference — or a bump of a baselined action to a different
unpinned ref — fails closed. Local ``./`` composite/reusable actions are exempt
(no ref to pin). This mirrors the repo's other ratchet gates (transport-mock,
no-noncanonical-lifecycle-classes).

SYNC with the pre-commit hook ``check-action-pins`` and the ci.yml step of the
same name.

Modes:
  (default)            fail if any non-exempt ``uses:`` is unpinned and NOT in
                       the baseline; also fail if a baselined entry has been
                       fixed (stale baseline — must be pruned, burn-down only).
  --update-baseline    rewrite the baseline from the current tree (use only when
                       intentionally freezing a new legitimate residual).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_BASELINE = Path(__file__).resolve().parent / "action_pin_baseline.txt"

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


def load_baseline(baseline: Path) -> set[str]:
    if not baseline.is_file():
        return set()
    out: set[str] = set()
    for line in baseline.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def write_baseline(baseline: Path, refs: set[str]) -> None:
    header = (
        "# Action-pin ratchet baseline (OMN-14762 / F-19-A) — burn-down only.\n"
        "# Frozen set of currently-unpinned `uses:` refs. NEW unpinned refs fail\n"
        "# closed (scripts/ci/check_action_pins.py). Remove an entry only by\n"
        "# pinning that action to a 40-char SHA. Do NOT add rows to silence a new\n"
        "# unpinned action — pin it instead.\n"
    )
    body = "".join(f"{r}\n" for r in sorted(refs))
    baseline.write_text(header + body, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(_REPO_ROOT))
    parser.add_argument("--baseline", default=str(_DEFAULT_BASELINE))
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    workflow_dir = root / ".github" / "workflows"
    baseline_path = Path(args.baseline).resolve()

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

    current_unpinned = unpinned_refs(workflow_dir)

    if args.update_baseline:
        write_baseline(baseline_path, current_unpinned)
        print(f"check-action-pins: wrote baseline with {len(current_unpinned)} ref(s)")
        return 0

    baseline = load_baseline(baseline_path)
    new_violations = sorted(current_unpinned - baseline)
    stale_baseline = sorted(baseline - current_unpinned)

    rc = 0
    if new_violations:
        rc = 1
        print(
            "check-action-pins: FAIL — NEW unpinned action reference(s):",
            file=sys.stderr,
        )
        for ref in new_violations:
            locs = [f"{f.relative_to(root)}:{ln}" for f, ln, v in all_uses if v == ref]
            print(f"  - {ref}  ({', '.join(locs)})", file=sys.stderr)
        print(
            "  Pin each to a 40-char commit SHA (e.g. actions/checkout@<sha> # v4).",
            file=sys.stderr,
        )
    if stale_baseline:
        rc = 1
        print(
            "check-action-pins: FAIL — baseline is STALE (these are now pinned/gone; "
            "prune them, burn-down only):",
            file=sys.stderr,
        )
        for ref in stale_baseline:
            print(f"  - {ref}", file=sys.stderr)

    if rc == 0:
        print(
            f"check-action-pins: OK — {len(current_unpinned)} baselined unpinned "
            f"ref(s), no new violations."
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
