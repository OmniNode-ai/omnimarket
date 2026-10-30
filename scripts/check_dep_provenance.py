#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Dependency provenance: PyPI releases or an internal sibling's dev merge tag.

A sibling git source is accepted without a suppression only when it names the
package's own repository and uses a dev merge tag matching the contract.
The rule and sibling list come from omnibase_core's packaged
validation/contracts/first_party_ref.validation.yaml (its wrong_repo and
first_party_source_not_dev_tag rules), which replaced the
contracts/internal_sibling_pins.yaml of OMN-19998. Run this script in the project environment (uv run --frozen).
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
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

_FIRST_PARTY_REF_CONTRACT = (
    "omnibase_core.validation.contracts",
    "first_party_ref.validation.yaml",
)
_SIBLINGS_RULE = "wrong_repo"
_DEV_TAG_RULE = "first_party_source_not_dev_tag"


class ModelSiblingPin(BaseModel):
    """One internal sibling: its distribution name and its own repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    distribution: str
    repo: str


class ModelSiblingPinPolicy(BaseModel):
    """Sibling list, GitHub org and dev merge tag form read from core's contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    github_org: str
    dev_tag_pattern: str
    siblings: tuple[ModelSiblingPin, ...]


# This script is also loaded by file path (spec_from_file_location), where the
# postponed annotation cannot be resolved from sys.modules; name it here.
ModelSiblingPinPolicy.model_rebuild(
    _types_namespace={"ModelSiblingPin": ModelSiblingPin}
)


def load_contract() -> ModelSiblingPinPolicy:
    """Read the sibling pin policy from omnibase_core's packaged first_party_ref contract."""
    package, name = _FIRST_PARTY_REF_CONTRACT
    document = yaml.safe_load(files(package).joinpath(name).read_text(encoding="utf-8"))
    rules = {
        rule["rule_id"]: rule["parameters"] for rule in document["validation"]["rules"]
    }
    siblings_rule = rules[_SIBLINGS_RULE]
    siblings = []
    for entry in siblings_rule["siblings"]:
        distribution, _, repo = str(entry).partition("=")
        if not distribution or not repo:
            raise ValueError(
                f"{name}: sibling entry {entry!r} is not distribution=repo"
            )
        siblings.append(ModelSiblingPin(distribution=distribution, repo=repo))
    return ModelSiblingPinPolicy(
        github_org=siblings_rule["github_org"],
        dev_tag_pattern=rules[_DEV_TAG_RULE]["tag_pattern"],
        siblings=tuple(siblings),
    )


_GIT_SOURCE_KEYS = frozenset({"git", "rev", "branch", "tag"})

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


def _is_sibling_dev_tag(
    pkg: str,
    attrs: dict[str, Any],
    sibling_repositories: dict[str, str],
    dev_tag_pattern: re.Pattern[str],
    github_org: str,
) -> bool:
    repo = sibling_repositories.get(pkg)
    if repo is None or _GIT_SOURCE_KEYS & attrs.keys() != {"git", "tag"}:
        return False
    tag = attrs.get("tag")
    git = attrs.get("git")
    return (
        isinstance(tag, str)
        and dev_tag_pattern.fullmatch(tag) is not None
        and isinstance(git, str)
        and git
        in {
            f"https://github.com/{github_org}/{repo}",
            f"https://github.com/{github_org}/{repo}.git",
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


def find_violations(
    text: str, contract: ModelSiblingPinPolicy | None = None
) -> list[str]:
    """Return diagnostic messages for each forbidden git-source override.

    An empty list means the file is clean (exit 0). A non-empty list means the
    gate fails (exit 1). Lines carrying a valid `# raw-override-ok: <token>`
    escape are excluded.
    """
    try:
        sources = tomllib.loads(text).get("tool", {}).get("uv", {}).get("sources", {})
    except tomllib.TOMLDecodeError as exc:
        return [f"invalid TOML: {exc}"]
    if contract is None:
        contract = load_contract()
    sibling_repositories = {
        _normalize(sibling.distribution): sibling.repo for sibling in contract.siblings
    }
    dev_tag_pattern = re.compile(contract.dev_tag_pattern)
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
            if not git_keys or _is_sibling_dev_tag(
                pkg, attrs, sibling_repositories, dev_tag_pattern, contract.github_org
            ):
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

    contract = load_contract()
    violations = find_violations(pyproject_path.read_text(), contract)

    if violations:
        print(
            "FAIL: forbidden first-party git-source override(s) in "
            f"{pyproject_path} [tool.uv.sources]:",
            file=sys.stderr,
        )
        for msg in violations:
            print(f"  - {msg}", file=sys.stderr)
        print(
            f"\nUse PyPI or a sibling's own https://github.com/{contract.github_org}/ "
            f"repository with a tag matching {contract.dev_tag_pattern}. "
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
