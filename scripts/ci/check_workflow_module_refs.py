#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Workflow module-resolution guard (OMN-14176).

Scans every GitHub Actions workflow under ``.github/workflows/`` for in-org
Python module references embedded in ``run:`` steps — ``from X import ...``,
``import X``, and ``python -m X`` invocations — and asserts that each *in-repo*
(``omnimarket.*``) reference resolves to a real module in ``src/omnimarket/``.

Why this exists
---------------
``.github/workflows/pr-review-bot.yml`` and the Phase 2 job of
``pr-arch-review.yml`` both imported ``omnimarket.nodes.node_pr_review_bot`` — a
module tree deleted in OMN-13212. Because both jobs short-circuit to a no-op
(``skipped_no_review_models``) when the ``PR_REVIEWER_MODELS`` repo var is empty,
the dead import never executed and CI stayed green while the workflow silently
did nothing and reported a false CLEAN. Nothing in CI proved that a module a
workflow claims to run still exists. This guard closes that gap: a workflow that
references a deleted in-repo module fails the required ``CI Summary`` rollup and
the pre-commit hook, so the class of bug is caught at the commit that introduces
it rather than months later.

Scope — same-repo only
----------------------
Only ``omnimarket.*`` references are resolved, against the local ``src/omnimarket``
tree. Sibling-repo references (``omniintelligence.*``, ``omnibase_*.*``, etc.) are
imported from clones that CI checks out at runtime — e.g. ``hostile-reviewer.yml``
runs ``python -m omniintelligence.review_pairing.cli_review`` against a
``../omniintelligence`` clone, NOT the omnimarket tree — so resolving them here
would false-positive on every legitimate cross-repo import. They are reported as
skipped for transparency. A cross-repo variant that resolves sibling refs against
their known clone paths is deferred to an OMN-14176 follow-up.

Resolution is a dependency-free filesystem check (module dotted path -> ``.py``
file or package ``__init__.py`` under ``src/``). It does not import the package,
so it runs without ``uv sync`` and cannot be defeated by an unrelated import-time
error.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

# scripts/ci/check_workflow_module_refs.py -> repo root is parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]

# Top-level package that lives in THIS repo. Refs under it are resolved locally.
IN_REPO_PACKAGE = "omnimarket"

# First-party org top-level packages. Anything matching one of these that is not
# ``omnimarket`` is a sibling-repo import resolved from a runtime clone and is
# therefore out of scope for this same-repo guard (reported, never failed).
ORG_PACKAGES: tuple[str, ...] = (
    "omnimarket",
    "omnibase_core",
    "omnibase_infra",
    "omnibase_spi",
    "omnibase_compat",
    "omniintelligence",
    "omniclaude",
    "omnimemory",
    "omnidash",
    "omniweb",
    "onex_change_control",
    "omninode_infra",
    "omninode_bridge",
    "omnistream",
    "omnicrush",
)

# Longest-first so a package name that is a prefix of another cannot short-match.
_PKG_ALT = "|".join(sorted(ORG_PACKAGES, key=len, reverse=True))
_MODULE = rf"(?:{_PKG_ALT})(?:\.[A-Za-z0-9_]+)*"

# Import surfaces. ``from X import`` requires the ``import`` keyword immediately
# after the module, so it never matches prose such as "... ABSENT from omnimarket
# CI ...". ``import X`` is guarded by a leading non-word/non-dot boundary so it
# does not match ``from a import b``'s tail or ``a.import`` attribute access.
# ``-m X`` covers ``python -m`` / ``uv run python -m`` / ``python3.12 -m``.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"\bfrom\s+({_MODULE})\s+import\b"),
    re.compile(rf"(?<![.\w])import\s+({_MODULE})\b"),
    re.compile(rf"-m\s+({_MODULE})\b"),
)


@dataclass(frozen=True)
class Ref:
    """A single in-org module reference found in a workflow file."""

    workflow: str
    line_no: int
    module: str
    text: str


def is_in_repo(module: str) -> bool:
    """True if ``module`` belongs to this repo's ``omnimarket`` package."""
    return module == IN_REPO_PACKAGE or module.startswith(IN_REPO_PACKAGE + ".")


def module_resolves(module: str, src_root: Path | None = None) -> bool:
    """Resolve a dotted module path to a file/package under ``src_root``.

    A module resolves if ``src/<parts>.py`` exists or ``src/<parts>/__init__.py``
    exists. No import is performed.
    """
    root = src_root if src_root is not None else REPO_ROOT / "src"
    base = root.joinpath(*module.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def extract_module_refs(text: str) -> list[tuple[int, str, str]]:
    """Extract ``(line_no, module, line_text)`` in-org refs from workflow text.

    Deduplicated per (line_no, module) so a line matching two patterns counts
    once.
    """
    seen: set[tuple[int, str]] = set()
    out: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern in _PATTERNS:
            for match in pattern.finditer(line):
                module = match.group(1)
                key = (line_no, module)
                if key in seen:
                    continue
                seen.add(key)
                out.append((line_no, module, line.strip()))
    return out


def collect_workflow_refs(workflows_dir: Path | None = None) -> list[Ref]:
    """Collect every in-org module ref across ``.github/workflows/*.y*ml``."""
    wdir = (
        workflows_dir
        if workflows_dir is not None
        else REPO_ROOT / ".github" / "workflows"
    )
    refs: list[Ref] = []
    for workflow in sorted(wdir.glob("*.y*ml")):
        for line_no, module, text in extract_module_refs(
            workflow.read_text(encoding="utf-8")
        ):
            refs.append(
                Ref(workflow=workflow.name, line_no=line_no, module=module, text=text)
            )
    return refs


def find_unresolved_in_repo_refs(
    workflows_dir: Path | None = None,
    src_root: Path | None = None,
) -> list[Ref]:
    """Return in-repo (``omnimarket.*``) refs that do NOT resolve on disk."""
    return [
        ref
        for ref in collect_workflow_refs(workflows_dir)
        if is_in_repo(ref.module) and not module_resolves(ref.module, src_root)
    ]


def main() -> int:
    refs = collect_workflow_refs()
    in_repo = [r for r in refs if is_in_repo(r.module)]
    sibling = [r for r in refs if not is_in_repo(r.module)]
    unresolved = [r for r in in_repo if not module_resolves(r.module)]

    print("Workflow module-resolution guard (OMN-14176)")
    print(f"  in-repo (omnimarket.*) refs checked : {len(in_repo)}")
    print(f"  sibling-repo refs skipped (out of scope): {len(sibling)}")

    if sibling:
        by_pkg: dict[str, int] = {}
        for ref in sibling:
            top = ref.module.split(".", 1)[0]
            by_pkg[top] = by_pkg.get(top, 0) + 1
        skipped = ", ".join(f"{pkg}={count}" for pkg, count in sorted(by_pkg.items()))
        print(f"    skipped by package: {skipped}")

    if not unresolved:
        print("\nPASS: every in-repo module referenced by a workflow resolves.")
        return 0

    print(f"\nFAIL: {len(unresolved)} unresolved in-repo module reference(s):")
    for ref in unresolved:
        print(f"  {ref.workflow}:{ref.line_no}: {ref.module}")
        print(f"      {ref.text}")
    print(
        "\nA workflow references an omnimarket module that does not exist under "
        "src/omnimarket/.\nEither the module was deleted (remove/repoint the "
        "workflow step) or the ref is a typo.\nDo not allowlist a dead reference."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
