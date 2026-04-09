#!/usr/bin/env python3
"""Generate multi-host adapter files for orchestrator nodes.

Reads metadata.yaml and contract.yaml for each orchestrator node and generates:
  - SKILL.md (Claude Code skill adapter)
  - .mdc (Cursor rules adapter)
  - instructions.md (Codex/generic adapter)
  - gemini.md (Gemini CLI adapter)

Only processes nodes where metadata.yaml declares node_role=orchestrator.
Output is deterministic: same input always produces the same output.

Usage:
    python scripts/generate_adapter.py
    python scripts/generate_adapter.py --node node_aislop_sweep
    python scripts/generate_adapter.py --output-dir /tmp/adapters
    python scripts/generate_adapter.py --formats gemini claude_code
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

NODES_DIR = Path(__file__).resolve().parent.parent / "src" / "omnimarket" / "nodes"
ADAPTERS_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "omnimarket" / "adapters"
)

SUPPORTED_FORMATS = ("claude_code", "cursor", "codex", "gemini")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _get_command_topic(contract: dict[str, Any]) -> str:
    try:
        return contract["event_bus"]["subscribe_topics"][0]
    except (KeyError, IndexError, TypeError):
        return "UNKNOWN_COMMAND_TOPIC"


def _get_completion_topic(contract: dict[str, Any]) -> str:
    try:
        return contract["event_bus"]["publish_topics"][0]
    except (KeyError, IndexError, TypeError):
        return "UNKNOWN_COMPLETION_TOPIC"


def _get_timeout_ms(contract: dict[str, Any]) -> int:
    try:
        return int(contract["descriptor"]["timeout_ms"])
    except (KeyError, TypeError, ValueError):
        return 60000


def _extract_args_table(contract: dict[str, Any]) -> str:
    """Build a markdown table of inputs from contract.yaml."""
    inputs = contract.get("inputs", {})
    if not inputs:
        return "| (no arguments) | — | — |"
    lines = []
    for field, spec in inputs.items():
        if not isinstance(spec, dict):
            continue
        desc = spec.get("description", "")
        default = spec.get("default", "—")
        lines.append(f"| {field} | {desc} | {default} |")
    return "\n".join(lines) if lines else "| (no arguments) | — | — |"


def _build_substitutions(
    node_name: str,
    metadata: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, str]:
    display_name = (
        metadata.get("display_name")
        or node_name.replace("node_", "").replace("_", " ").title()
    )
    description = metadata.get("description", f"Run {display_name} via OmniMarket.")
    entry_flags = metadata.get("entry_flags") or []
    flags_str = " ".join(entry_flags) if entry_flags else ""
    skill_slug = node_name.replace("node_", "").replace("_", "-")

    return {
        "SKILL_DISPLAY_NAME": display_name,
        "NODE_NAME": node_name,
        "NODE_DIR": node_name,
        "SKILL_SLUG": skill_slug,
        "SKILL_DESCRIPTION": description,
        "TRIGGER_DESCRIPTION": description.rstrip(".").lower(),
        "COMMAND_TOPIC": _get_command_topic(contract),
        "COMPLETION_TOPIC": _get_completion_topic(contract),
        "TIMEOUT_MS": str(_get_timeout_ms(contract)),
        "ARGS_TABLE": _extract_args_table(contract),
        "ENTRY_FLAGS": flags_str,
        "CATEGORY": metadata.get("pack", "omnimarket"),
        "TAG_1": metadata.get("pack", "omnimarket"),
        "TAG_2": skill_slug,
        # Placeholder args — override via metadata.entry_flags or contract.inputs
        "ARG_1": "dry_run",
        "ARG_1_DESCRIPTION": "Report only — no side effects",
        "ARG_1_DEFAULT": "false",
        "ARG_2": "repos",
        "ARG_2_DESCRIPTION": "Target repositories (comma-separated)",
        "ARG_2_DEFAULT": "all",
        "PAYLOAD_FIELD_1": "dry_run",
        "EXAMPLE_VALUE_1": "true",
        "PAYLOAD_FIELD_2": "repos",
        "EXAMPLE_VALUE_2": '["omniclaude"]',
        "GLOB_PATTERN_1": "**/*.py",
        "GLOB_PATTERN_2": "**/*.ts",
        "EXAMPLE_USER_INTENT_1": f"run {skill_slug}",
        "EXAMPLE_USER_INTENT_2": f"run {skill_slug} dry run",
        "FIELD": "dry_run",
        "VALUE": "true",
    }


def _apply_substitutions(template_text: str, subs: dict[str, str]) -> str:
    result = template_text
    for key, value in subs.items():
        result = result.replace(f"{{{{{key}}}}}", value)
    return result


def _read_template(adapter_type: str) -> str:
    if adapter_type == "claude_code":
        tmpl = ADAPTERS_DIR / "claude_code" / "template_SKILL.md"
    elif adapter_type == "cursor":
        tmpl = ADAPTERS_DIR / "cursor" / "template.mdc"
    elif adapter_type == "codex":
        tmpl = ADAPTERS_DIR / "codex" / "template.md"
    elif adapter_type == "gemini":
        tmpl = ADAPTERS_DIR / "gemini" / "template.md"
    else:
        raise ValueError(f"Unknown adapter type: {adapter_type}")
    return tmpl.read_text()


def _output_filename(adapter_type: str, skill_slug: str) -> str:
    if adapter_type == "claude_code":
        return f"{skill_slug.replace('-', '_')}_SKILL.md"
    if adapter_type == "cursor":
        return f"{skill_slug}.mdc"
    if adapter_type == "codex":
        return f"{skill_slug}-instructions.md"
    if adapter_type == "gemini":
        return f"{skill_slug}.md"
    raise ValueError(f"Unknown adapter type: {adapter_type}")


def generate_adapters(
    node_dir: Path,
    output_dir: Path,
    formats: tuple[str, ...] = SUPPORTED_FORMATS,
) -> list[Path]:
    metadata_path = node_dir / "metadata.yaml"
    contract_path = node_dir / "contract.yaml"

    if not metadata_path.exists() or not contract_path.exists():
        return []

    metadata = _load_yaml(metadata_path)
    contract = _load_yaml(contract_path)

    node_role = metadata.get("node_role", "")
    if node_role != "orchestrator":
        return []

    node_name = node_dir.name
    subs = _build_substitutions(node_name, metadata, contract)
    skill_slug = subs["SKILL_SLUG"]

    generated: list[Path] = []
    for fmt in formats:
        if fmt not in SUPPORTED_FORMATS:
            continue
        template_text = _read_template(fmt)
        content = _apply_substitutions(template_text, subs)
        filename = _output_filename(fmt, skill_slug)
        out_path = output_dir / fmt / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content)
        generated.append(out_path)

    return generated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate multi-host adapter files for orchestrator nodes."
    )
    parser.add_argument(
        "--node", help="Process only this node (e.g. node_aislop_sweep)"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ADAPTERS_DIR,
        help="Root output directory (default: src/omnimarket/adapters/)",
    )
    parser.add_argument(
        "--formats",
        nargs="+",
        choices=SUPPORTED_FORMATS,
        default=list(SUPPORTED_FORMATS),
        help="Adapter formats to generate",
    )
    args = parser.parse_args(argv)

    if args.node:
        node_dirs = [NODES_DIR / args.node]
    else:
        node_dirs = sorted(d for d in NODES_DIR.iterdir() if d.is_dir())

    total = 0
    for node_dir in node_dirs:
        generated = generate_adapters(node_dir, args.output_dir, tuple(args.formats))
        for path in generated:
            print(
                f"  generated: {path.relative_to(args.output_dir.parent.parent.parent.parent)}"
            )
            total += 1

    print(f"\n{total} adapter file(s) generated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
