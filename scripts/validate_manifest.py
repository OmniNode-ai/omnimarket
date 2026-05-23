#!/usr/bin/env python3
"""Validate ground_truth_manifest.yaml schema and hash integrity.

Usage:
    uv run python scripts/validate_manifest.py <manifest_path> [--omni-home <path>]

Exit 0 if all checks pass, exit 1 if any fail.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import yaml

REQUIRED_FIELDS = {
    "id",
    "root_paths",
    "ground_truth_adr",
    "ground_truth_adr_hash",
    "source_file_hash",
    "manifest_schema_version",
    "models",
    "expected_decision_types",
    "expected_keywords",
}

EXPECTED_SCHEMA_VERSION = "v1"


def sha256_of_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def sha256_of_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def validate_manifest(manifest_path: Path, omni_home: Path) -> int:
    with manifest_path.open() as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "entries" not in raw:
        print("FAIL: manifest top-level must be a dict with 'entries' key")
        return 1

    entries: list[dict] = raw["entries"]
    print(f"Loaded {len(entries)} entries from {manifest_path}\n")

    failures: list[str] = []
    seen_ids: set[str] = set()

    for i, entry in enumerate(entries):
        entry_id = entry.get("id", f"<entry-{i}>")
        prefix = f"[{entry_id}]"
        entry_failures: list[str] = []

        # 1. Required fields
        missing = REQUIRED_FIELDS - set(entry.keys())
        if missing:
            entry_failures.append(f"missing required fields: {sorted(missing)}")

        # 2. Unique id
        if entry_id in seen_ids:
            entry_failures.append("duplicate id")
        seen_ids.add(entry_id)

        # 3. manifest_schema_version
        if entry.get("manifest_schema_version") != EXPECTED_SCHEMA_VERSION:
            entry_failures.append(
                f"manifest_schema_version is {entry.get('manifest_schema_version')!r},"
                f" expected {EXPECTED_SCHEMA_VERSION!r}"
            )

        # 4. ground_truth_adr_hash matches inline text
        adr_text: str | None = entry.get("ground_truth_adr")
        adr_hash_stored: str | None = entry.get("ground_truth_adr_hash")
        if adr_text and adr_hash_stored:
            computed_adr_hash = sha256_of_text(adr_text)
            if computed_adr_hash != adr_hash_stored:
                entry_failures.append(
                    f"ground_truth_adr_hash mismatch: stored={adr_hash_stored!r},"
                    f" computed={computed_adr_hash!r}"
                )

        # 5. source_file_hash matches first resolvable root_path file/dir
        root_paths: list[str] = entry.get("root_paths", [])
        source_hash_stored: str | None = entry.get("source_file_hash")
        if source_hash_stored and root_paths:
            # The source_file_hash is computed from the first root_path that resolves
            # to a file; for directories we hash the sorted concatenation of all files.
            first_path = omni_home / root_paths[0]
            if first_path.exists():
                if first_path.is_file():
                    computed_source_hash = sha256_of_file(first_path)
                else:
                    # Directory: hash concatenated sorted file contents
                    files = sorted(first_path.rglob("*"))
                    files = [f for f in files if f.is_file()]
                    combined = b""
                    for fp in files:
                        combined += fp.read_bytes()
                    computed_source_hash = (
                        "sha256:" + hashlib.sha256(combined).hexdigest()
                    )

                if computed_source_hash != source_hash_stored:
                    # Document mismatch but don't hard-fail (source may have changed since manifest creation)
                    entry_failures.append(
                        f"source_file_hash mismatch (source may have changed):"
                        f" stored={source_hash_stored!r}, computed={computed_source_hash!r}"
                    )
            else:
                entry_failures.append(f"root_path does not exist on disk: {first_path}")

        # 6. Low confidence entries must have curation_notes
        if entry.get("source_confidence") == "low" and not entry.get("curation_notes"):
            entry_failures.append("source_confidence=low but curation_notes is missing")

        if entry_failures:
            for msg in entry_failures:
                print(f"FAIL {prefix}: {msg}")
            failures.extend([f"{prefix}: {msg}" for msg in entry_failures])
        else:
            print(f"PASS {prefix}")

    print(f"\n{'=' * 60}")
    print(f"Entries checked : {len(entries)}")
    print(f"Failures        : {len(failures)}")

    if failures:
        print("\nFailed checks:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("All checks passed.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate ground_truth_manifest.yaml")
    parser.add_argument("manifest_path", type=Path)
    parser.add_argument(
        "--omni-home",
        type=Path,
        default=Path("/Users/jonah/Code/omni_home"),
        help="Path to omni_home repo root (default: /Users/jonah/Code/omni_home)",
    )
    args = parser.parse_args()

    sys.exit(validate_manifest(args.manifest_path, args.omni_home))


if __name__ == "__main__":
    main()
