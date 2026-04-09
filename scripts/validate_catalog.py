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


def run(
    root: Path,
    catalog_path: Path | None,
    check_pack: bool,
    check_naming: bool,
    check_catalog: bool,
    verbose: bool = False,
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

    if all_errors:
        print(f"CATALOG VALIDATION FAILED: {len(all_errors)} violation(s) found\n")
        for error in all_errors:
            print(f"  {error}")
        return 1

    node_count = len(metadata_files)
    print(
        f"catalog validate: OK ({node_count} nodes, {sum([check_pack, check_naming, check_catalog])} checks)"
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
        args.check_pack_fields or args.check_naming or args.check_catalog_fresh
    )

    sys.exit(
        run(
            root=args.root,
            catalog_path=args.catalog,
            check_pack=args.check_pack_fields or run_all,
            check_naming=args.check_naming or run_all,
            check_catalog=args.check_catalog_fresh or run_all,
            verbose=args.verbose,
        )
    )


if __name__ == "__main__":
    main()
