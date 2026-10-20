#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Dependency provenance: PyPI releases or an internal sibling's dev merge tag.

A sibling git source is accepted without a suppression only when it names the
package's own OmniNode repository and uses ``tag = "dev-<14 digits>-<12 hex>"``.
Branches, raw revisions and foreign repositories do not qualify. The existing
per-line temporary override annotation remains supported.

This static, offline check validates the declared source, not remote tag
existence. The sibling compatibility gate verifies the resolved lock commit.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

# Package names and repository slugs differ for the omninode-* distributions.
_SIBLING_REPOSITORIES = {
    "omnibase-core": "omnibase_core",
    "omnibase-spi": "omnibase_spi",
    "omnibase-compat": "omnibase_compat",
    "omnibase-infra": "omnibase_infra",
    "omninode-memory": "omnimemory",
    "omninode-intelligence": "omniintelligence",
    "omninode-claude": "omniclaude",
}
_GIT_SOURCE_KEYS = frozenset({"git", "rev", "branch", "tag"})
_DEV_MERGE_TAG = re.compile(r"dev-[0-9]{14}-[0-9a-f]{12}")

# Inline escape token: `# raw-override-ok: <ticket>` with a non-empty token.
_ESCAPE_TOKEN_RE = re.compile(r"#\s*raw-override-ok:\s*(\S+)")

# Raw section text is needed only for the existing per-line annotation.
_UVS_BLOCK_RE = re.compile(
    r"^\[tool\.uv\.sources\](.*?)(?=^\[|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _normalize(pkg: str) -> str:
    """Canonicalize a package name to hyphen form for comparison."""
    return pkg.strip().strip('"').strip("'").replace("_", "-").lower()


def _uv_sources_block(text: str) -> str | None:
    """Return the raw text of the [tool.uv.sources] block, or None if absent."""
    block_m = _UVS_BLOCK_RE.search(text)
    return block_m.group(1) if block_m else None


def _is_sibling_dev_tag(pkg: str, attrs: dict[str, Any]) -> bool:
    repo = _SIBLING_REPOSITORIES.get(pkg)
    if repo is None or _GIT_SOURCE_KEYS & attrs.keys() != {"git", "tag"}:
        return False
    tag = attrs.get("tag")
    git = attrs.get("git")
    return (
        isinstance(tag, str)
        and _DEV_MERGE_TAG.fullmatch(tag) is not None
        and isinstance(git, str)
        and git
        in {
            f"https://github.com/OmniNode-ai/{repo}",
            f"https://github.com/OmniNode-ai/{repo}.git",
        }
    )


def _line_for_package(block: str, pkg: str) -> str | None:
    """Return the raw source line (with any trailing comment) declaring `pkg`."""
    for raw_line in block.splitlines():
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        # Entry key is the text before the first '=' on the line.
        key = stripped.split("=", 1)[0]
        if _normalize(key) == pkg:
            return raw_line
    return None


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------


def find_violations(text: str) -> list[str]:
    """Return diagnostic messages for each forbidden git-source override.

    An empty list means the file is clean (exit 0). A non-empty list means the
    gate fails (exit 1). Lines carrying a valid `# raw-override-ok: <token>`
    escape are excluded.
    """
    try:
        sources = tomllib.loads(text).get("tool", {}).get("uv", {}).get("sources", {})
    except tomllib.TOMLDecodeError as exc:
        return [f"invalid TOML: {exc}"]
    block = _uv_sources_block(text) or ""
    violations: list[str] = []

    for name, source in sources.items():
        pkg = _normalize(name)
        # Preserve the pre-existing immutable-main policy for this distribution.
        if pkg == "onex-change-control":
            continue
        candidates = source if isinstance(source, list) else [source]
        for attrs in candidates:
            if not isinstance(attrs, dict):
                continue
            git_keys = sorted(_GIT_SOURCE_KEYS & attrs.keys())
            if not git_keys or _is_sibling_dev_tag(pkg, attrs):
                continue
            raw_line = _line_for_package(block, pkg)
            if raw_line is not None and _ESCAPE_TOKEN_RE.search(raw_line):
                continue
            keys_desc = ", ".join(f"{key}={attrs[key]!r}" for key in git_keys)
            violations.append(
                f"{pkg}: forbidden git-source override ({keys_desc}). "
                "Use PyPI or the package's own OmniNode sibling dev merge tag. "
                f"line: {raw_line.strip() if raw_line else '<unresolved>'}"
            )
    return violations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--pyproject",
        default="pyproject.toml",
        help="Path to pyproject.toml (default: pyproject.toml)",
    )
    args = parser.parse_args(argv)

    pyproject_path = Path(args.pyproject)
    if not pyproject_path.exists():
        print(
            f"ERROR: pyproject.toml not found: {pyproject_path}",
            file=sys.stderr,
        )
        return 1

    violations = find_violations(pyproject_path.read_text())

    if violations:
        print(
            "FAIL: forbidden first-party git-source override(s) in "
            f"{pyproject_path} [tool.uv.sources]:",
            file=sys.stderr,
        )
        for msg in violations:
            print(f"  - {msg}", file=sys.stderr)
        print(
            "\nUse PyPI or a sibling's own https://github.com/OmniNode-ai/ "
            "repository with tag=dev-<14 digits>-<12 lowercase hex>. "
            "Branches, bare revisions and foreign git sources are refused. "
            "A temporary override requires a non-empty per-line annotation.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: no forbidden first-party git-source override in "
        f"{pyproject_path} [tool.uv.sources]."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
