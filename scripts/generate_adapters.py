#!/usr/bin/env python3
"""Generate multi-host adapter files for orchestrator nodes.

Reads metadata.yaml and contract.yaml for each node under src/omnimarket/nodes/,
filters to nodes with node_role=orchestrator, and generates:
  - adapters/claude_code/{slug}_SKILL.md
  - adapters/cursor/{slug}.mdc
  - adapters/codex/{slug}-instructions.md

Usage:
    python scripts/generate_adapters.py
    python scripts/generate_adapters.py --dry-run
    python scripts/generate_adapters.py --node node_ticket_pipeline
    python scripts/generate_adapters.py --output-dir /path/to/output
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snake_to_kebab(name: str) -> str:
    return name.replace("_", "-")


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
        topics = contract["event_bus"]["publish_topics"]
        # Prefer the terminal_event topic if declared, otherwise last publish topic
        terminal = contract.get("terminal_event")
        if terminal:
            return terminal
        return topics[-1]
    except (KeyError, IndexError, TypeError):
        return "UNKNOWN_COMPLETION_TOPIC"


def _get_timeout_ms(contract: dict[str, Any]) -> int:
    try:
        return int(contract["descriptor"]["timeout_ms"])
    except (KeyError, TypeError, ValueError):
        return 120000


def _build_args_table(entry_flags: dict[str, str]) -> str:
    """Build a markdown table from entry_flags dict (key=flag, value=description)."""
    if not entry_flags:
        return "| (none) | — | — |\n"
    rows = []
    for flag, description in entry_flags.items():
        rows.append(f"| {flag} | {description} | — |")
    return "\n".join(rows) + "\n"


def _build_args_frontmatter(entry_flags: dict[str, str]) -> str:
    """Build SKILL.md frontmatter args block from entry_flags dict."""
    if not entry_flags:
        return "  # No entry flags declared\n"
    lines = []
    for flag, description in entry_flags.items():
        lines.append(f"  - name: {flag}")
        lines.append(f'    description: "{description}"')
        lines.append("    required: false")
    return "\n".join(lines) + "\n"


def _build_cli_examples(slug: str, entry_flags: dict[str, str]) -> str:
    lines = [f"/{slug}                    # Default invocation"]
    for flag in list(entry_flags.keys())[:2]:
        lines.append(f"/{slug} {flag}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Template renderers
# ---------------------------------------------------------------------------


def _render_skill_md(
    *,
    node_name: str,
    slug: str,
    display_name: str,
    description: str,
    pack: str,
    entry_flags: dict[str, str],
    command_topic: str,
    completion_topic: str,
    timeout_ms: int,
    tags: list[str],
) -> str:
    tag1 = tags[0] if len(tags) > 0 else slug
    tag2 = tags[1] if len(tags) > 1 else pack
    args_block = _build_args_frontmatter(entry_flags)
    cli_block = _build_cli_examples(slug, entry_flags)

    return f"""\
---
description: "{description}"
version: 1.0.0
mode: full
level: advanced
debug: false
category: "{pack}"
tags:
  - omnimarket
  - "{tag1}"
  - "{tag2}"
author: OmniMarket
composable: true
args:
{args_block}
inputs:
  - name: correlation_id
    description: "UUID v4 for event correlation"
outputs:
  - name: skill_result
    description: "Completion event payload from the OmniMarket node"
---

# {display_name} (OmniMarket)

## Overview

Thin event-bus wrapper around the OmniMarket `{node_name}` node. This skill
publishes a command event and monitors for completion — all business logic
executes in the node handler.

**Announce at start:** "Running {slug} via OmniMarket event bus."

## Execution

### Step 1 — Assemble payload

Collect arguments from the user invocation and build the command payload:

```json
{{
  "correlation_id": "<uuid4>"
}}
```

Omit fields the user did not specify — the node applies its own defaults.

### Step 2 — Publish command event

Publish to topic: `{command_topic}`

Source: `contract.yaml → event_bus.subscribe_topics[0]`

### Step 3 — Monitor completion

Listen on topic: `{completion_topic}`

Source: `contract.yaml → event_bus.publish_topics[-1]` (or `terminal_event`)

Filter by `correlation_id`. Timeout: **{timeout_ms} ms** (from contract `descriptor.timeout_ms`).

### Step 4 — Format output

On success, render the completion payload in a format appropriate for the skill's
output type. On timeout or error, report the failure clearly.

## CLI

```
{cli_block}
```

## Important

This wrapper contains **no business logic**. Do not add domain logic here.
All processing is handled by the `{node_name}` node in
`omnimarket/nodes/{node_name}/`.
"""


def _render_mdc(
    *,
    node_name: str,
    slug: str,
    display_name: str,
    description: str,
    entry_flags: dict[str, str],
    command_topic: str,
    completion_topic: str,
    timeout_ms: int,
) -> str:
    first_flag = next(iter(entry_flags), None)
    payload_comment = (
        '  "' + first_flag + '": "<value>"' if first_flag else "  // no flags"
    )
    return f"""\
---
description: "{description}"
globs:
  - "**/*.py"
  - "**/contract.yaml"
alwaysApply: false
---

# {display_name} (OmniMarket)

When the user asks to run {slug} or invoke the {display_name}, follow this procedure.
**Do not implement the logic yourself** — delegate to the OmniMarket node via the event bus.

## Step 1 — Assemble payload

Collect any user-specified options and build the command payload:

```json
{{
  "correlation_id": "<generate a UUID v4>",
  {payload_comment}
}}
```

Omit fields the user did not specify — the node applies its own defaults.

## Step 2 — Publish command event

Publish to the ONEX event bus:
- **Topic:** `{command_topic}`
- **Payload:** The assembled JSON from Step 1

Source: `contract.yaml → event_bus.subscribe_topics[0]`

## Step 3 — Monitor completion

Listen on the ONEX event bus:
- **Topic:** `{completion_topic}`
- **Filter:** Match `correlation_id` from Step 1
- **Timeout:** {timeout_ms} ms

Source: `contract.yaml → terminal_event` or `event_bus.publish_topics[-1]`

## Step 4 — Format output

On success, render the completion payload in a clear markdown format.
On timeout: report that the operation timed out.
On error: surface the error message from the completion event payload.

## Important

This rule contains **no business logic**. All processing executes in the
`{node_name}` OmniMarket node. This rule only handles event publish/subscribe
and output formatting.
"""


def _render_instructions_md(
    *,
    node_name: str,
    slug: str,
    display_name: str,
    description: str,
    entry_flags: dict[str, str],
    command_topic: str,
    completion_topic: str,
    timeout_ms: int,
) -> str:
    args_table = _build_args_table(entry_flags)
    return f"""\
# {display_name} — Instructions

You have access to the OmniMarket `{node_name}` node via the ONEX event bus.
When the user asks you to run {slug} or {description.lower().rstrip(".")},
use this procedure. **Do not implement the logic yourself.**

## Supported arguments

| Argument | Description | Default |
|----------|-------------|---------|
{args_table}
## Procedure

### Step 1 — Assemble payload

Build a JSON payload from the user's request:

```json
{{
  "correlation_id": "<generate a UUID v4>"
}}
```

Only include fields the user explicitly specified. The node applies defaults for
omitted fields.

### Step 2 — Publish command event

Publish to the ONEX event bus:
- **Topic:** `{command_topic}`
- **Payload:** The JSON from Step 1

Source: `contract.yaml → event_bus.subscribe_topics[0]`

### Step 3 — Monitor completion

Listen on the ONEX event bus:
- **Topic:** `{completion_topic}`
- **Filter:** Match the `correlation_id` from Step 1
- **Timeout:** {timeout_ms} ms

Source: `contract.yaml → terminal_event` or `event_bus.publish_topics[-1]`

### Step 4 — Format output

On success: render the completion payload in a clear format for the user.

On timeout: report that the operation timed out.

On error: surface the error message from the completion event payload.

## Important

Do not implement any business logic. All processing runs in the OmniMarket
`{node_name}` node. These instructions only cover event publish/subscribe and
output formatting.
"""


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def discover_orchestrator_nodes(
    nodes_dir: Path, filter_node: str | None = None
) -> list[tuple[Path, dict[str, Any], dict[str, Any]]]:
    """Return list of (node_dir, metadata, contract) for orchestrator nodes."""
    results = []
    for node_dir in sorted(nodes_dir.iterdir()):
        if not node_dir.is_dir():
            continue
        if node_dir.name.startswith("__"):
            continue
        if filter_node and node_dir.name != filter_node:
            continue

        meta_path = node_dir / "metadata.yaml"
        contract_path = node_dir / "contract.yaml"

        if not meta_path.exists():
            continue

        metadata = _load_yaml(meta_path)

        # Only generate for nodes with node_role=orchestrator
        node_role = metadata.get("node_role", "")
        if node_role != "orchestrator":
            continue

        contract: dict[str, Any] = {}
        if contract_path.exists():
            contract = _load_yaml(contract_path)

        results.append((node_dir, metadata, contract))

    return results


def generate_adapters_for_node(
    node_dir: Path,
    metadata: dict[str, Any],
    contract: dict[str, Any],
    output_dir: Path,
    dry_run: bool = False,
) -> dict[str, Path]:
    """Generate all three adapter files for a single node. Returns paths written."""
    node_name = node_dir.name
    slug = _snake_to_kebab(node_name.removeprefix("node_"))
    display_name = metadata.get("display_name") or slug.replace("-", " ").title()
    description = metadata.get("description", f"OmniMarket {display_name} node")
    pack = metadata.get("pack", "omnimarket")
    entry_flags: dict[str, str] = metadata.get("entry_flags") or {}
    tags: list[str] = metadata.get("tags") or []

    command_topic = _get_command_topic(contract)
    completion_topic = _get_completion_topic(contract)
    timeout_ms = _get_timeout_ms(contract)

    shared_kwargs = {
        "node_name": node_name,
        "slug": slug,
        "display_name": display_name,
        "description": description,
        "entry_flags": entry_flags,
        "command_topic": command_topic,
        "completion_topic": completion_topic,
        "timeout_ms": timeout_ms,
    }

    skill_content = _render_skill_md(
        pack=pack,
        tags=tags,
        **shared_kwargs,
    )
    mdc_content = _render_mdc(**shared_kwargs)
    instructions_content = _render_instructions_md(**shared_kwargs)

    claude_dir = output_dir / "claude_code"
    cursor_dir = output_dir / "cursor"
    codex_dir = output_dir / "codex"

    skill_path = claude_dir / f"{slug}_SKILL.md"
    mdc_path = cursor_dir / f"{slug}.mdc"
    instructions_path = codex_dir / f"{slug}-instructions.md"

    if not dry_run:
        claude_dir.mkdir(parents=True, exist_ok=True)
        cursor_dir.mkdir(parents=True, exist_ok=True)
        codex_dir.mkdir(parents=True, exist_ok=True)

        skill_path.write_text(skill_content)
        mdc_path.write_text(mdc_content)
        instructions_path.write_text(instructions_content)

    return {
        "skill_md": skill_path,
        "mdc": mdc_path,
        "instructions_md": instructions_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate multi-host adapter files for orchestrator nodes."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log actions without writing files",
    )
    parser.add_argument(
        "--node",
        metavar="NODE_NAME",
        default=None,
        help="Generate adapters for a single named node only (e.g. node_ticket_pipeline)",
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        default=str(ADAPTERS_DIR),
        help=f"Output directory for generated adapters (default: {ADAPTERS_DIR})",
    )
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    nodes = discover_orchestrator_nodes(NODES_DIR, filter_node=args.node)

    if not nodes:
        print(
            "No orchestrator nodes found"
            + (f" matching '{args.node}'" if args.node else "")
            + ". Add node_role: orchestrator to a node's metadata.yaml to generate adapters."
        )
        return 0

    prefix = "[DRY RUN] " if args.dry_run else ""
    generated = 0
    for node_dir, metadata, contract in nodes:
        paths = generate_adapters_for_node(
            node_dir=node_dir,
            metadata=metadata,
            contract=contract,
            output_dir=output_dir,
            dry_run=args.dry_run,
        )
        print(f"{prefix}Generated adapters for {node_dir.name}:")
        for kind, path in paths.items():
            print(f"  {kind}: {path}")
        generated += 1

    print(f"\n{prefix}Total: {generated} node(s) processed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
