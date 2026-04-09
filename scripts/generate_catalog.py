"""Catalog generator script for OmniMarket node catalog.

Walks all metadata.yaml files across the repo, groups nodes by pack field,
filters internal nodes (node_role=internal), flags deprecated nodes, and
outputs both catalog.yaml (machine-readable) and catalog.md (human-readable).

Usage:
    python scripts/generate_catalog.py [--root PATH] [--output-dir PATH]

Fields used from metadata.yaml (all optional for backward compatibility):
    pack: str           -- domain grouping (from OMN-8077)
    display_name: str   -- human-friendly name (from OMN-8077)
    node_role: str      -- "internal" nodes are excluded from public catalog (from OMN-8079)
    deprecated: bool    -- whether the node is deprecated (from OMN-8080)
    deprecation_reason: str  -- reason for deprecation (from OMN-8080)
    deprecation_since: str   -- version since deprecated (from OMN-8080)
    replacement: str    -- replacement node name (from OMN-8080)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml


def find_metadata_files(root: Path) -> list[Path]:
    """Walk root recursively and return all metadata.yaml file paths."""
    return sorted(root.rglob("metadata.yaml"))


def load_metadata(path: Path) -> dict[str, Any] | None:
    """Load and return a metadata.yaml as a dict. Returns None on parse error."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def is_internal(metadata: dict[str, Any]) -> bool:
    """Return True if node_role is 'internal'."""
    return str(metadata.get("node_role", "")).lower() == "internal"


def is_deprecated(metadata: dict[str, Any]) -> bool:
    """Return True if deprecated field is truthy."""
    return bool(metadata.get("deprecated", False))


def get_deprecation_info(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """Extract deprecation info block if the node is deprecated."""
    if not is_deprecated(metadata):
        return None
    info: dict[str, Any] = {}
    if metadata.get("deprecation_reason"):
        info["reason"] = metadata["deprecation_reason"]
    if metadata.get("deprecation_since"):
        info["since"] = metadata["deprecation_since"]
    if metadata.get("replacement"):
        info["replacement"] = metadata["replacement"]
    return info


def build_node_entry(metadata: dict[str, Any], source_path: Path) -> dict[str, Any]:
    """Build a catalog entry dict from metadata."""
    entry: dict[str, Any] = {
        "name": metadata["name"],
        "version": metadata.get("version", ""),
        "description": metadata.get("description", ""),
    }
    display_name = metadata.get("display_name")
    if display_name:
        entry["display_name"] = display_name

    tags = metadata.get("tags", [])
    if tags:
        entry["tags"] = tags

    capabilities = metadata.get("capabilities", {})
    if capabilities:
        entry["capabilities"] = capabilities

    deprecation = get_deprecation_info(metadata)
    if deprecation is not None:
        entry["deprecated"] = True
        if deprecation:
            entry["deprecation_info"] = deprecation

    entry["source"] = str(source_path)
    return entry


def group_nodes_by_pack(
    nodes: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group node entries by their pack field. Nodes without pack go to 'uncategorized'."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        pack = node.get("pack", "uncategorized")
        groups.setdefault(pack, []).append(node)
    # Sort within each pack by node name for idempotence
    for pack in groups:
        groups[pack] = sorted(groups[pack], key=lambda n: n["name"])
    return dict(sorted(groups.items()))


def generate_catalog_yaml(
    nodes_by_pack: dict[str, list[dict[str, Any]]],
    metadata_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build the catalog.yaml structure."""
    packs: list[dict[str, Any]] = []
    for pack_name, nodes in nodes_by_pack.items():
        pack_entry: dict[str, Any] = {"name": pack_name, "nodes": []}
        for node in nodes:
            # Reconstruct clean node entry without 'pack' key and without 'source' key
            node_entry: dict[str, Any] = {
                "name": node["name"],
                "version": node["version"],
                "description": node["description"],
            }
            if node.get("display_name"):
                node_entry["display_name"] = node["display_name"]
            if node.get("tags"):
                node_entry["tags"] = node["tags"]
            if node.get("capabilities"):
                node_entry["capabilities"] = node["capabilities"]
            if node.get("deprecated"):
                node_entry["deprecated"] = True
                if node.get("deprecation_info"):
                    node_entry["deprecation_info"] = node["deprecation_info"]
            pack_entry["nodes"].append(node_entry)
        packs.append(pack_entry)

    return {
        "schema_version": "1.0",
        "generated_by": "generate_catalog.py",
        "packs": packs,
    }


def generate_catalog_md(nodes_by_pack: dict[str, list[dict[str, Any]]]) -> str:
    """Build the catalog.md human-readable content."""
    lines: list[str] = [
        "# OmniMarket Node Catalog",
        "",
        "> Auto-generated by `scripts/generate_catalog.py`. Do not edit manually.",
        "",
    ]

    total_nodes = sum(len(nodes) for nodes in nodes_by_pack.values())
    deprecated_count = sum(
        1 for nodes in nodes_by_pack.values() for n in nodes if n.get("deprecated")
    )
    lines += [
        f"**Total nodes:** {total_nodes}",
        f"**Deprecated:** {deprecated_count}",
        f"**Packs:** {len(nodes_by_pack)}",
        "",
        "---",
        "",
    ]

    for pack_name, nodes in nodes_by_pack.items():
        lines.append(f"## {pack_name}")
        lines.append("")
        for node in nodes:
            display = node.get("display_name") or node["name"]
            deprecated_tag = " _(deprecated)_" if node.get("deprecated") else ""
            lines.append(f"### {display}{deprecated_tag}")
            lines.append("")
            lines.append(f"**Node:** `{node['name']}` | **Version:** {node['version']}")
            lines.append("")
            if node.get("description"):
                lines.append(node["description"])
                lines.append("")
            if node.get("tags"):
                tags_str = ", ".join(f"`{t}`" for t in node["tags"])
                lines.append(f"**Tags:** {tags_str}")
                lines.append("")
            if node.get("deprecated") and node.get("deprecation_info"):
                info = node["deprecation_info"]
                lines.append("**Deprecation:**")
                if info.get("since"):
                    lines.append(f"- Since: {info['since']}")
                if info.get("reason"):
                    lines.append(f"- Reason: {info['reason']}")
                if info.get("replacement"):
                    lines.append(f"- Replacement: `{info['replacement']}`")
                lines.append("")

    return "\n".join(lines) + "\n"


def run(
    root: Path,
    output_dir: Path,
    verbose: bool = False,
) -> int:
    """Main entry point. Returns exit code (0=success, 1=error)."""
    metadata_files = find_metadata_files(root)

    if verbose:
        print(f"Found {len(metadata_files)} metadata.yaml files under {root}")

    nodes: list[dict[str, Any]] = []
    skipped_internal = 0
    parse_errors = 0
    metadata_by_name: dict[str, dict[str, Any]] = {}

    for path in metadata_files:
        metadata = load_metadata(path)
        if metadata is None:
            parse_errors += 1
            if verbose:
                print(f"  WARN: Failed to parse {path}", file=sys.stderr)
            continue

        if "name" not in metadata:
            if verbose:
                print(
                    f"  WARN: Skipping {path} — missing 'name' field", file=sys.stderr
                )
            continue

        if is_internal(metadata):
            skipped_internal += 1
            if verbose:
                print(f"  SKIP (internal): {metadata['name']}")
            continue

        entry = build_node_entry(metadata, path)
        # Carry pack through for grouping (not written to final catalog nodes)
        entry["pack"] = metadata.get("pack", "uncategorized")
        nodes.append(entry)
        metadata_by_name[metadata["name"]] = metadata

    # Group by pack
    nodes_by_pack = group_nodes_by_pack(nodes)

    # Generate outputs
    catalog_yaml_data = generate_catalog_yaml(nodes_by_pack, metadata_by_name)
    catalog_md_content = generate_catalog_md(nodes_by_pack)

    output_dir.mkdir(parents=True, exist_ok=True)

    catalog_yaml_path = output_dir / "catalog.yaml"
    catalog_md_path = output_dir / "catalog.md"

    with open(catalog_yaml_path, "w") as f:
        yaml.dump(
            catalog_yaml_data,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    with open(catalog_md_path, "w") as f:
        f.write(catalog_md_content)

    print("Catalog generated:")
    print(f"  Nodes: {len(nodes)} public ({skipped_internal} internal filtered)")
    print(f"  Packs: {len(nodes_by_pack)}")
    print(f"  Deprecated: {sum(1 for n in nodes if n.get('deprecated'))}")
    if parse_errors:
        print(f"  Parse errors: {parse_errors} (see warnings above)")
    print(f"  Output: {catalog_yaml_path}")
    print(f"  Output: {catalog_md_path}")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate OmniMarket node catalog from metadata.yaml files."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).parent.parent / "src",
        help="Root directory to search for metadata.yaml files (default: ../src)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent.parent / "catalog",
        help="Output directory for catalog.yaml and catalog.md (default: ../catalog)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed progress output",
    )
    args = parser.parse_args()
    sys.exit(run(args.root, args.output_dir, args.verbose))


if __name__ == "__main__":
    main()
