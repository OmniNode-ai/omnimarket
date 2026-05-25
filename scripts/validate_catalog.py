#!/usr/bin/env python3
"""Catalog completeness validator for OmniMarket node catalog.

CI validator that ensures catalog completeness:
- Every metadata.yaml has a non-null pack field (no ungrouped nodes)
- Node naming follows pack_role_qualifier convention
- catalog.yaml is up-to-date (regenerating produces no diff)

Usage:
    python scripts/validate_catalog.py [--root PATH] [--catalog PATH]
    python scripts/validate_catalog.py --check-pack-fields
    python scripts/validate_catalog.py --check-naming
    python scripts/validate_catalog.py --check-catalog-fresh

Exit codes:
    0: All checks pass
    1: One or more violations found
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

# Node naming convention: name must match node_{pack}_{role}_{qualifier} or node_{pack}_{role}
# where qualifier is optional. Both pack and role are required parts (after the node_ prefix).
# Minimum: node_<something>_<something>
_MIN_PARTS = 3  # node + pack + role

_SKILL_LLM_ALLOWLIST_RE = re.compile(
    r"#\s*onex-allow-skill-llm-boundary\s+OMN-[0-9]+\s+reason=\"[^\"]+\""
)

_SKILL_LLM_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "CONCRETE_MODEL_ID",
        re.compile(
            r"\b("
            r"gpt-(?:[0-9]|4|5|[A-Za-z0-9_.-]+)|"
            r"o[134](?:-[A-Za-z0-9_.-]+)?|"
            r"claude-(?:haiku|sonnet|opus|[0-9])[A-Za-z0-9_.-]*|"
            r"gemini-[0-9][A-Za-z0-9_.-]*|"
            r"glm-[0-9][A-Za-z0-9_.-]*|"
            r"(?:qwen|Qwen)[A-Za-z0-9_.:/-]*|"
            r"(?:deepseek|DeepSeek)[A-Za-z0-9_.:/-]*|"
            r"(?:cyankiwi|Corianas|mlx-community)/[A-Za-z0-9_.:/-]+"
            r")\b"
        ),
    ),
    (
        "MODEL_CONFIG_FIELD",
        re.compile(r"\b(?:served_model_id|model_id|default_model|api_base|base_url)\b"),
    ),
    (
        "MODEL_ENV_DEFAULT",
        re.compile(r"\bLLM_[A-Z0-9_]+_(?:URL|MODEL|MODEL_NAME)\b"),
    ),
    (
        "DIRECT_LLM_ENDPOINT_URL",
        re.compile(
            r"https?://(?:api\.openai\.com|api\.anthropic\.com|"
            r"generativelanguage\.googleapis\.com|open\.bigmodel\.cn|"
            r"localhost(?::[0-9]+)?/v1|127\.0\.0\.1(?::[0-9]+)?/v1)"
        ),
    ),
)

_SKILL_LLM_BOUNDARY_PATHS = (
    Path("plugins/onex/.codex-plugin/plugin.json"),
    Path("plugins/onex/skills"),
    Path("src/omnimarket/adapters/codex/skills"),
    Path("src/omnimarket/adapters/codex/template.md"),
    Path("src/omnimarket/adapters/claude_code/template_SKILL.md"),
    Path("src/omnimarket/adapters/claude_code/aislop_sweep_SKILL.md"),
    Path("src/omnimarket/adapters/cursor"),
    Path("src/omnimarket/adapters/gemini"),
)

_SKILL_LLM_EXTENSIONS = {".json", ".md", ".mdc"}


def find_metadata_files(root: Path) -> list[Path]:
    """Walk root recursively and return all metadata.yaml file paths under nodes/."""
    nodes_dir = root / "nodes"
    if not nodes_dir.exists():
        # Fallback: search root directly
        return sorted(root.rglob("metadata.yaml"))
    return sorted(nodes_dir.rglob("metadata.yaml"))


def load_yaml_file(path: Path) -> dict[str, Any] | None:
    """Load a YAML file as a dict. Returns None on parse error."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def check_pack_fields(metadata_files: list[Path]) -> list[str]:
    """Check that every metadata.yaml has a non-null pack field.

    Returns list of error strings (empty = all pass).
    """
    errors: list[str] = []
    for path in metadata_files:
        data = load_yaml_file(path)
        if data is None:
            errors.append(f"PARSE_ERROR: Cannot parse {path}")
            continue
        pack = data.get("pack")
        if not pack:
            node_name = data.get("name", str(path.parent.name))
            errors.append(
                f"MISSING_PACK: Node '{node_name}' ({path}) has no pack field. "
                f"Every node must belong to a pack. "
                f"Add: pack: <domain-pack-name>"
            )
    return errors


def check_node_naming(metadata_files: list[Path]) -> list[str]:
    """Check that node names follow the pack_role_qualifier naming convention.

    Convention: name must start with 'node_' and contain at least pack + role
    as underscore-separated segments after the 'node_' prefix.

    Returns list of error strings (empty = all pass).
    """
    errors: list[str] = []
    for path in metadata_files:
        data = load_yaml_file(path)
        if data is None:
            continue  # Parse errors reported by check_pack_fields
        name = data.get("name", "")
        if not name:
            errors.append(f"MISSING_NAME: {path} has no name field")
            continue
        parts = name.split("_")
        if parts[0] != "node":
            errors.append(
                f"NAMING_CONVENTION: Node '{name}' ({path}) must start with 'node_'. "
                f"Expected: node_<pack>_<role>[_<qualifier>]"
            )
            continue
        if len(parts) < _MIN_PARTS:
            errors.append(
                f"NAMING_CONVENTION: Node '{name}' ({path}) has only {len(parts)} "
                f"underscore-separated segments (minimum {_MIN_PARTS}). "
                f"Expected: node_<pack>_<role>[_<qualifier>]"
            )
    return errors


def check_catalog_fresh(
    catalog_path: Path,
    root: Path,
    generate_script: Path,
) -> list[str]:
    """Check that catalog.yaml is up-to-date by regenerating and diffing.

    Returns list of error strings (empty = catalog is fresh).
    """
    errors: list[str] = []

    if not catalog_path.exists():
        errors.append(
            f"CATALOG_MISSING: {catalog_path} does not exist. "
            f"Run: python {generate_script} --output-dir {catalog_path.parent}"
        )
        return errors

    if not generate_script.exists():
        # Cannot check freshness without the generator — skip
        return []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_output = Path(tmpdir)
        result = subprocess.run(
            [
                sys.executable,
                str(generate_script),
                "--root",
                str(root),
                "--output-dir",
                str(tmp_output),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            errors.append(
                f"CATALOG_REGEN_FAILED: Could not regenerate catalog. "
                f"Stderr: {result.stderr.strip()}"
            )
            return errors

        regen_path = tmp_output / "catalog.yaml"
        if not regen_path.exists():
            errors.append(
                "CATALOG_REGEN_FAILED: Regenerated catalog.yaml not found in tmp dir"
            )
            return errors

        current_text = catalog_path.read_text()
        regen_text = regen_path.read_text()

        if current_text != regen_text:
            errors.append(
                f"CATALOG_STALE: {catalog_path} is out of date. "
                f"Regenerating produces a diff. "
                f"Run: python {generate_script} --output-dir {catalog_path.parent}"
            )

    return errors


def _repo_root_for_catalog_root(root: Path) -> Path:
    resolved = root.resolve()
    if resolved.name == "omnimarket" and resolved.parent.name == "src":
        return resolved.parents[1]
    return resolved


def _iter_skill_llm_boundary_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for relative_path in _SKILL_LLM_BOUNDARY_PATHS:
        path = repo_root / relative_path
        if path.is_file() and path.suffix in _SKILL_LLM_EXTENSIONS:
            files.append(path)
        elif path.is_dir():
            files.extend(
                sorted(
                    child
                    for child in path.rglob("*")
                    if child.is_file() and child.suffix in _SKILL_LLM_EXTENSIONS
                )
            )
    return sorted(set(files))


def check_skill_llm_boundary(repo_root: Path) -> list[str]:
    """Reject concrete LLM routing truth in active skill/catalog surfaces.

    Skills are thin runtime shims. They may describe logical routing needs when
    backed by a node contract, but they must not own provider model IDs, LLM
    endpoint URLs, model config fields, or silent helper defaults.
    """

    errors: list[str] = []
    for path in _iter_skill_llm_boundary_files(repo_root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(lines, start=1):
            if _SKILL_LLM_ALLOWLIST_RE.search(line):
                continue
            for category, pattern in _SKILL_LLM_FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    relative = path.relative_to(repo_root)
                    errors.append(
                        f"SKILL_LLM_BOUNDARY:{category}: {relative}:{line_no}: "
                        "skills/catalog adapters must use logical routing needs "
                        "and must not own concrete model IDs, endpoint URLs, or "
                        "fallback model defaults"
                    )
                    break
    return errors


def run(
    root: Path,
    catalog_path: Path | None,
    check_pack: bool,
    check_naming: bool,
    check_catalog: bool,
    verbose: bool = False,
    check_skill_llm: bool = False,
) -> int:
    """Main validation logic. Returns exit code (0=pass, 1=violations found)."""
    scripts_dir = Path(__file__).parent
    generate_script = scripts_dir / "generate_catalog.py"

    metadata_files = find_metadata_files(root)

    if not metadata_files:
        print(f"WARN: No metadata.yaml files found under {root}", file=sys.stderr)
        return 0

    if verbose:
        print(f"Found {len(metadata_files)} metadata.yaml files under {root}")

    all_errors: list[str] = []

    if check_pack:
        errors = check_pack_fields(metadata_files)
        all_errors.extend(errors)
        if verbose and not errors:
            print("  PASS: pack field check")

    if check_naming:
        errors = check_node_naming(metadata_files)
        all_errors.extend(errors)
        if verbose and not errors:
            print("  PASS: naming convention check")

    if check_catalog:
        if catalog_path is None:
            # Default: catalog/catalog.yaml relative to repo root
            catalog_path = root.parent / "catalog" / "catalog.yaml"
        errors = check_catalog_fresh(catalog_path, root, generate_script)
        all_errors.extend(errors)
        if verbose and not errors:
            print("  PASS: catalog freshness check")

    if check_skill_llm:
        repo_root = _repo_root_for_catalog_root(root)
        errors = check_skill_llm_boundary(repo_root)
        all_errors.extend(errors)
        if verbose and not errors:
            print("  PASS: skill LLM boundary check")

    if all_errors:
        print(f"CATALOG VALIDATION FAILED: {len(all_errors)} violation(s) found\n")
        for error in all_errors:
            print(f"  {error}")
        return 1

    node_count = len(metadata_files)
    print(
        "catalog validate: OK "
        f"({node_count} nodes, "
        f"{sum([check_pack, check_naming, check_catalog, check_skill_llm])} checks)"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate OmniMarket node catalog completeness."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).parent.parent / "src" / "omnimarket",
        help="Root src directory containing nodes/ (default: ../src/omnimarket)",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Path to catalog.yaml to check for freshness (default: ../catalog/catalog.yaml)",
    )
    parser.add_argument(
        "--check-pack-fields",
        action="store_true",
        default=False,
        help="Check that every metadata.yaml has a non-null pack field",
    )
    parser.add_argument(
        "--check-naming",
        action="store_true",
        default=False,
        help="Check that node names follow pack_role_qualifier convention",
    )
    parser.add_argument(
        "--check-catalog-fresh",
        action="store_true",
        default=False,
        help="Check that catalog.yaml is up-to-date",
    )
    parser.add_argument(
        "--check-skill-llm-boundary",
        action="store_true",
        default=False,
        help=(
            "Check active skill/plugin adapter surfaces for hardcoded LLM "
            "model IDs, endpoint URLs, and helper defaults"
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Run all checks (default when no specific check is selected)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed output",
    )
    args = parser.parse_args()

    # If no specific check selected, run all
    run_all = args.all or not (
        args.check_pack_fields
        or args.check_naming
        or args.check_catalog_fresh
        or args.check_skill_llm_boundary
    )

    sys.exit(
        run(
            root=args.root,
            catalog_path=args.catalog,
            check_pack=args.check_pack_fields or run_all,
            check_naming=args.check_naming or run_all,
            check_catalog=args.check_catalog_fresh or run_all,
            verbose=args.verbose,
            check_skill_llm=args.check_skill_llm_boundary or run_all,
        )
    )


if __name__ == "__main__":
    main()
