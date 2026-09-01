#!/usr/bin/env python3
"""Generate multi-host adapter files for orchestrator nodes.

Reads metadata.yaml and contract.yaml for each node under src/omnimarket/nodes/,
filters to nodes with node_role=orchestrator, and generates:
  - adapters/claude_code/{slug}_SKILL.md
  - adapters/cursor/{slug}.mdc
  - adapters/codex/skills/{slug}/SKILL.md

Usage:
    python scripts/generate_adapters.py
    python scripts/generate_adapters.py --dry-run
    python scripts/generate_adapters.py --node node_ticket_pipeline
    python scripts/generate_adapters.py --output-dir /path/to/output
    python scripts/generate_adapters.py --canonical-codex-skills --check
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from omnimarket.adapters.codex.topics import (
    TOPIC_CODEX_PATTERN_B_DISPATCH_COMMAND,
    TOPIC_CODEX_PATTERN_B_DISPATCH_COMPLETED,
)

NODES_DIR = Path(__file__).resolve().parent.parent / "src" / "omnimarket" / "nodes"
ADAPTERS_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "omnimarket" / "adapters"
)

RUNTIME_TRUTH_BEGIN = "<!-- BEGIN GENERATED CODEX RUNTIME TRUTH: do not edit -->"
RUNTIME_TRUTH_END = "<!-- END GENERATED CODEX RUNTIME TRUTH -->"
NATIVE_PR_LIFECYCLE_COMMAND = "pr_lifecycle_orchestrator"
CONTRACTS_MANIFEST_ROOT = Path("src/omnimarket/nodes")


@dataclass(frozen=True)
class CodexSkillSpec:
    """Canonical identity for one shipped Codex skill."""

    slug: str
    command_name: str
    node_name: str
    contract_path: str
    transport_route: Literal["pattern_b", "native_contract"] = "pattern_b"


# This is the authoritative adapter inventory. Do not infer it from node
# roles, contract names, or directory names: those are node metadata, not the
# public Codex command surface.
CODEX_SKILL_SPECS: tuple[CodexSkillSpec, ...] = (
    CodexSkillSpec(
        "adversarial-pipeline",
        "adversarial_pipeline_orchestrator",
        "node_adversarial_pipeline_orchestrator",
        "src/omnimarket/nodes/node_adversarial_pipeline_orchestrator/contract.yaml",
    ),
    CodexSkillSpec(
        "aislop-sweep",
        "aislop_sweep",
        "node_aislop_sweep",
        "src/omnimarket/nodes/node_aislop_sweep/contract.yaml",
    ),
    CodexSkillSpec(
        "bus-audit",
        "bus_audit_compute",
        "node_bus_audit_compute",
        "src/omnimarket/nodes/node_bus_audit_compute/contract.yaml",
    ),
    CodexSkillSpec(
        "coderabbit-triage",
        "coderabbit_triage",
        "node_coderabbit_triage",
        "src/omnimarket/nodes/node_coderabbit_triage/contract.yaml",
    ),
    CodexSkillSpec(
        "dep-cascade-dedup",
        "dep_cascade_dedup_orchestrator",
        "node_dep_cascade_dedup_orchestrator",
        "src/omnimarket/nodes/node_dep_cascade_dedup_orchestrator/contract.yaml",
    ),
    CodexSkillSpec(
        "gap",
        "gap_compute",
        "node_gap_compute",
        "src/omnimarket/nodes/node_gap_compute/contract.yaml",
    ),
    CodexSkillSpec(
        "local-review",
        "local_review",
        "node_local_review",
        "src/omnimarket/nodes/node_local_review/contract.yaml",
    ),
    CodexSkillSpec(
        "merge-sweep",
        "pr_lifecycle_orchestrator",
        "node_pr_lifecycle_orchestrator",
        "src/omnimarket/nodes/node_pr_lifecycle_orchestrator/contract.yaml",
        "native_contract",
    ),
    CodexSkillSpec(
        "observability-sink",
        "observability_sink_effect",
        "node_observability_sink_effect",
        "src/omnimarket/nodes/node_observability_sink_effect/contract.yaml",
    ),
    CodexSkillSpec(
        "pr-polish",
        "pr_polish",
        "node_pr_polish",
        "src/omnimarket/nodes/node_pr_polish/contract.yaml",
    ),
    CodexSkillSpec(
        "recall",
        "recall_compute",
        "node_recall_compute",
        "src/omnimarket/nodes/node_recall_compute/contract.yaml",
    ),
    CodexSkillSpec(
        "session-bootstrap",
        "session_bootstrap",
        "node_session_bootstrap",
        "src/omnimarket/nodes/node_session_bootstrap/contract.yaml",
    ),
    CodexSkillSpec(
        "session-orchestrator",
        "session_orchestrator",
        "node_session_orchestrator",
        "src/omnimarket/nodes/node_session_orchestrator/contract.yaml",
    ),
    CodexSkillSpec(
        "ticket-pipeline",
        "ticket_pipeline",
        "node_ticket_pipeline",
        "src/omnimarket/nodes/node_ticket_pipeline/contract.yaml",
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_runtime_truth_block(
    *,
    node_name: str,
    node_alias: str,
    contract_command_topic: str,
    contract_terminal_topic: str,
    contract_command_name: str | None = None,
    transport_route: Literal["pattern_b", "native_contract"] | None = None,
) -> str:
    """Render the generator-owned transport/target-contract truth block."""
    contract_name = contract_command_name or node_alias
    # ``None`` keeps the legacy all-node renderer compatible.  Canonical
    # Codex generation always supplies the explicit manifest route.
    route = transport_route or (
        "native_contract" if node_alias == NATIVE_PR_LIFECYCLE_COMMAND else "pattern_b"
    )
    if route not in {"pattern_b", "native_contract"}:
        raise ValueError(f"Unsupported Codex adapter transport route: {route!r}")
    if route == "native_contract":
        transport = f"""- **Route:** native target-node contract route (the adapter selects
  the node contract command and terminal topics for this command).
- **Adapter transport command topic:** `{contract_command_topic}`
- **Adapter transport response topic:** `{contract_terminal_topic}`"""
    else:
        transport = f"""- **Route:** generic Pattern-B adapter transport.
- **Adapter transport command topic:** `{TOPIC_CODEX_PATTERN_B_DISPATCH_COMMAND}`
- **Adapter transport response topic:** `{TOPIC_CODEX_PATTERN_B_DISPATCH_COMPLETED}`"""

    return f"""{RUNTIME_TRUTH_BEGIN}
## Runtime truth (generator-owned)

This section is generated from the Codex adapter contract and the target node
contract. Keep the two surfaces distinct:

### Codex adapter transport

- **Command name:** `{node_alias}`
- **Request wrapper:** `scripts/run_codex_runtime_request.py`
{transport}
- **Compile-only:** pass `--compile-only` to validate the request and binding
  without publishing an event or starting a runtime. This is adapter preflight,
  not evidence that a target runtime executed the command.
- **Runtime evidence:** inspect `runtime_evidence.runtime_observation` and
  `runtime_evidence.adapter_dispatch_binding`; compile-only is `UNOBSERVED`
  with reason `compile_only`.
- **Evidence wire schema:** `runtime_evidence.schema_version` is
  `runtime-evidence/v2`; v2 requires `runtime_observation` and carries the
  resolved node contract under `adapter_dispatch_binding.node_contract`.
- **Binding fields:** `adapter_dispatch_binding` reports
  `adapter_command_topic`, `requested_response_topic`,
  `selected_terminal_topic`, and `terminal_selection` (`NODE_CONTRACT`,
  `DIRECT_DELEGATE_SKILL_CONTRACT`, or `EXPLICIT_RESPONSE_OVERRIDE`); its
  `node_contract` is the resolved, typed contract binding.

### Target node contract metadata

- **Backing node:** `{node_name}`
- **Contract command name:** `{contract_name}`
- **Contract command topic:** `{contract_command_topic}`
- **Contract terminal topic:** `{contract_terminal_topic}`

The skill must use the request wrapper for dispatch. It must not publish the
target node command topic directly as its generic adapter transport; the target
topics above are contract metadata selected by the runtime adapter.
{RUNTIME_TRUTH_END}
"""


def _snake_to_kebab(name: str) -> str:
    return name.replace("_", "-")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _validate_codex_skill_specs(
    specs: tuple[CodexSkillSpec, ...], contracts_root: Path
) -> list[tuple[CodexSkillSpec, Path, dict[str, Any]]]:
    """Resolve and validate the explicit Codex inventory before writing files."""
    slugs = [spec.slug for spec in specs]
    commands = [spec.command_name for spec in specs]
    nodes = [spec.node_name for spec in specs]
    contract_paths = [spec.contract_path for spec in specs]
    if len(slugs) != len(set(slugs)):
        raise ValueError("Codex skill manifest contains duplicate slugs")
    if len(commands) != len(set(commands)):
        raise ValueError("Codex skill manifest contains duplicate command names")
    if len(nodes) != len(set(nodes)):
        raise ValueError("Codex skill manifest contains duplicate nodes")
    if len(contract_paths) != len(set(contract_paths)):
        raise ValueError("Codex skill manifest contains duplicate contract paths")

    resolved: list[tuple[CodexSkillSpec, Path, dict[str, Any]]] = []
    for spec in specs:
        if not spec.slug or not spec.command_name or not spec.node_name:
            raise ValueError(f"Codex skill manifest has incomplete entry: {spec!r}")
        if spec.transport_route not in {"pattern_b", "native_contract"}:
            raise ValueError(f"Codex skill manifest has invalid route: {spec!r}")
        manifest_path = Path(spec.contract_path)
        if (
            manifest_path.is_absolute()
            or ".." in manifest_path.parts
            or manifest_path.name != "contract.yaml"
            or len(manifest_path.parts) < 2
            or not manifest_path.is_relative_to(CONTRACTS_MANIFEST_ROOT)
            or manifest_path.parts[-2] != spec.node_name
        ):
            raise ValueError(
                f"Codex skill {spec.slug} has invalid repo-relative contract path"
            )
        contract_path = contracts_root / manifest_path.relative_to(
            CONTRACTS_MANIFEST_ROOT
        )
        if not contract_path.is_file():
            raise FileNotFoundError(
                f"Codex skill {spec.slug} references missing contract: {contract_path}"
            )
        contract = _load_yaml(contract_path)
        command_topic = _get_command_topic(contract)
        terminal_topic = _get_completion_topic(contract)
        if command_topic.startswith("UNKNOWN_") or terminal_topic.startswith(
            "UNKNOWN_"
        ):
            raise ValueError(f"Codex skill {spec.slug} contract has no runtime topics")
        resolved.append((spec, contract_path, contract))
    return resolved


def _upsert_runtime_truth_block(content: str, block: str) -> str:
    """Replace only generator-owned truth, preserving hand-authored skill prose."""
    if RUNTIME_TRUTH_BEGIN in content or RUNTIME_TRUTH_END in content:
        if (
            content.count(RUNTIME_TRUTH_BEGIN) != 1
            or content.count(RUNTIME_TRUTH_END) != 1
        ):
            raise ValueError(
                "skill has malformed generator-owned runtime-truth markers"
            )
        start = content.index(RUNTIME_TRUTH_BEGIN)
        end = content.index(RUNTIME_TRUTH_END, start) + len(RUNTIME_TRUTH_END)
        # Keep the existing separator after the end marker.  Adding a new
        # newline here would make every idempotent run grow the file by one
        # blank line, especially for complete files rendered by this module.
        return content[:start] + block.rstrip("\n") + content[end:]
    insertion = content.find("## Arguments")
    if insertion < 0:
        raise ValueError("skill has no ## Arguments insertion point")
    return content[:insertion] + block + "\n" + content[insertion:]


def _render_canonical_codex_skill(
    spec: CodexSkillSpec, contract: dict[str, Any]
) -> str:
    """Render a complete canonical skill when the destination does not exist."""
    inputs = contract.get("inputs") if isinstance(contract.get("inputs"), dict) else {}
    return _render_instructions_md(
        node_name=spec.node_name,
        node_alias=spec.command_name,
        slug=spec.slug,
        display_name=spec.slug.replace("-", " ").title(),
        description=str(contract.get("description", f"OmniMarket {spec.command_name}")),
        entry_flags=_derive_entry_flags(inputs),
        command_topic=_get_command_topic(contract),
        completion_topic=_get_completion_topic(contract),
        timeout_ms=_get_timeout_ms(contract),
        contract_command_name=str(contract.get("name", "")).strip() or spec.node_name,
        transport_route=spec.transport_route,
    )


def generate_canonical_codex_skills(
    output_dir: Path,
    *,
    contracts_root: Path = NODES_DIR,
    specs: tuple[CodexSkillSpec, ...] = CODEX_SKILL_SPECS,
    dry_run: bool = False,
    check: bool = False,
    preserve_existing: bool = True,
) -> tuple[Path, ...]:
    """Generate/check exactly the manifest's canonical ``<slug>/SKILL.md`` paths."""
    resolved = _validate_codex_skill_specs(specs, contracts_root)
    expected_paths = {output_dir / spec.slug / "SKILL.md" for spec, _, _ in resolved}
    existing_paths = set(output_dir.rglob("SKILL.md")) if output_dir.exists() else set()
    extras = existing_paths - expected_paths
    if extras:
        raise ValueError(
            "canonical Codex output contains unlisted skill paths: "
            + ", ".join(str(path) for path in sorted(extras))
        )

    drift: list[Path] = []
    for spec, _, contract in resolved:
        path = output_dir / spec.slug / "SKILL.md"
        block = _render_runtime_truth_block(
            node_name=spec.node_name,
            node_alias=spec.command_name,
            contract_command_name=str(contract.get("name", "")).strip()
            or spec.node_name,
            contract_command_topic=_get_command_topic(contract),
            contract_terminal_topic=_get_completion_topic(contract),
            transport_route=spec.transport_route,
        )
        current = path.read_text() if path.exists() else None
        if current is not None and preserve_existing:
            desired = _upsert_runtime_truth_block(current, block)
        else:
            desired = _render_canonical_codex_skill(spec, contract)
        if current != desired:
            drift.append(path)
            if not dry_run and not check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(desired)
    if check and drift:
        raise RuntimeError(
            "Codex skill runtime-truth drift detected: "
            + ", ".join(str(path) for path in drift)
        )
    return tuple(sorted(expected_paths))


def _get_command_topic(contract: dict[str, Any]) -> str:
    try:
        return contract["event_bus"]["subscribe_topics"][0]
    except (KeyError, IndexError, TypeError):
        return "UNKNOWN_COMMAND_TOPIC"


def _get_completion_topic(contract: dict[str, Any]) -> str:
    try:
        # Prefer the terminal_event topic if declared, otherwise last publish topic
        terminal = contract.get("terminal_event")
        if terminal:
            return terminal
        topics = contract["event_bus"]["publish_topics"]
        return topics[-1]
    except (KeyError, IndexError, TypeError):
        return "UNKNOWN_COMPLETION_TOPIC"


def _get_timeout_ms(contract: dict[str, Any]) -> int:
    try:
        return int(contract["descriptor"]["timeout_ms"])
    except (KeyError, TypeError, ValueError):
        return 120000


def _get_timeout_seconds(contract: dict[str, Any]) -> int:
    timeout_ms = _get_timeout_ms(contract)
    return max(1, (timeout_ms + 999) // 1000)


def _build_args_table(entry_flags: dict[str, str]) -> str:
    """Build a markdown table from entry_flags dict (key=flag, value=description)."""
    if not entry_flags:
        return "| (none) | — | — |\n"
    rows = []
    for flag, description in entry_flags.items():
        rows.append(f"| {flag} | {description} | — |")
    return "\n".join(rows) + "\n"


def _build_contract_inputs_table(contract_inputs: dict[str, Any]) -> str:
    """Build a markdown table from contract inputs."""
    rows = []
    for field, spec in contract_inputs.items():
        if field == "correlation_id":
            continue
        if not isinstance(spec, dict):
            continue
        description = str(spec.get("description", ""))
        default = spec.get("default", "—")
        rows.append(f"| {field} | {description} | {default} |")
    if not rows:
        return "| (none) | — | — |\n"
    return "\n".join(rows) + "\n"


def _derive_entry_flags(contract_inputs: dict[str, Any]) -> dict[str, str]:
    """Derive a user-facing flag map when metadata omits entry_flags."""
    derived: dict[str, str] = {}
    for field, spec in contract_inputs.items():
        if field == "correlation_id":
            continue
        if not isinstance(spec, dict):
            continue
        description = str(spec.get("description", "")).strip() or "Contract input"
        derived[field] = description
    return derived


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


def _entry_flag_to_cli(flag: str) -> str:
    if flag.startswith("--"):
        return flag
    return f"--{flag.replace('_', '-')}"


def _build_dispatch_payload_example(entry_flags: dict[str, str]) -> str:
    payload_lines = ['  "correlation_id": "<uuid4>"']
    for index, flag in enumerate(entry_flags):
        normalized = flag.lstrip("-").replace("-", "_")
        separator = "," if index < len(entry_flags) - 1 else ""
        payload_lines.append(f'  "{normalized}": "<value>"{separator}')
    if len(payload_lines) > 1:
        payload_lines[0] += ","
    return "{\n" + "\n".join(payload_lines) + "\n}"


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

Collect arguments from the user invocation with the shared wrapper helpers:

```python
from omnimarket.adapters.wrapper_base import (
    collect_args,
    generate_correlation_id,
    map_args_to_payload,
    validate_args,
)
```

Build the command payload:

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

Do not add concrete LLM provider names, served model IDs, endpoint URLs, or
fallback model defaults to this skill. If the backing node requires LLM work,
the skill may pass logical routing needs only when those fields are declared in
the node contract; runtime model resolution belongs to `node_model_router` and
contract/overlay policy.
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

```python
from omnimarket.adapters.wrapper_base import (
    collect_args,
    generate_correlation_id,
    map_args_to_payload,
    validate_args,
)
```

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
Use `format_output`, `handle_timeout`, `handle_error`, `stream_progress`, and
`check_environment` from `omnimarket.adapters.wrapper_base` for wrapper-owned
formatting, progress, error, timeout, and environment diagnostics.

## Important

This rule contains **no business logic**. All processing executes in the
`{node_name}` OmniMarket node. This rule only handles event publish/subscribe
and output formatting.

Do not add concrete LLM provider names, served model IDs, endpoint URLs, or
fallback model defaults to this skill. If the backing node requires LLM work,
the skill may pass logical routing needs only when those fields are declared in
the node contract; runtime model resolution belongs to `node_model_router` and
contract/overlay policy.
"""


def _render_instructions_md(
    *,
    node_name: str,
    node_alias: str,
    slug: str,
    display_name: str,
    description: str,
    entry_flags: dict[str, str],
    command_topic: str,
    completion_topic: str,
    timeout_ms: int,
    contract_command_name: str | None = None,
    transport_route: Literal["pattern_b", "native_contract"] | None = None,
) -> str:
    args_table = _build_args_table(entry_flags)
    payload_example = _build_dispatch_payload_example(entry_flags)
    runtime_truth = _render_runtime_truth_block(
        node_name=node_name,
        node_alias=node_alias,
        contract_command_topic=command_topic,
        contract_terminal_topic=completion_topic,
        contract_command_name=contract_command_name,
        transport_route=transport_route,
    )
    return f"""\
---
name: {slug}
description: Thin Codex skill shim for the OmniMarket {node_name} node. Use when the user asks to run {slug}.
---

# {display_name}

You have access to the OmniMarket `{node_name}` node through the Codex runtime request adapter.
When the user asks you to run {slug} or {description.lower().rstrip(".")},
use this procedure. Do not implement the node logic yourself.

{runtime_truth}

## Supported arguments

| Argument | Description | Default |
|----------|-------------|---------|
{args_table}
## Procedure

### Step 1 - Build JSON payload

Map user-provided arguments into a JSON object that matches the backing node's
input model. Omit fields the user did not specify so the node can apply its
own defaults. Adapter wrappers share `collect_args`, `validate_args`,
`map_args_to_payload`, `generate_correlation_id`, `format_output`,
`handle_timeout`, `handle_error`, `stream_progress`, and `check_environment`
from `omnimarket.adapters.wrapper_base`.

Use this dispatch shape:

```json
{payload_example}
```

### Step 2 - Dispatch through the Codex runtime request adapter

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \\
  --command-name "{node_alias}" \\
  --payload '<json-payload>' \\
  --timeout-ms {timeout_ms}
```

The command prints a JSON response object to stdout.

### Step 3 - Interpret the response

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result and render that clearly for the user.

If `ok` is `true` and `output_payloads` is absent, fall back to rendering
`dispatch_result`.

If `ok` is `false`, surface `error.code` and `error.message` directly.

If a dry run depends on GitHub or Linear and those systems are unreachable,
report that degraded condition explicitly rather than inventing remote state.

Do not add concrete LLM provider names, served model IDs, endpoint URLs, or
fallback model defaults to this skill. If the backing node requires LLM work,
pass logical routing needs only when those fields are declared in the node
contract; runtime model resolution belongs to `node_model_router` and
contract/overlay policy.

### Step 4 - Format output

On success: prefer `output_payloads[0]`; if it is absent, render the runtime
`dispatch_result`.

On timeout: report that the operation timed out.

On error: surface the runtime adapter error code and message.

## Contract

- Backing node: `omnimarket/nodes/{node_name}/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `{node_alias}`
- Target contract command topic: `{command_topic}`
- Target contract terminal topic: `{completion_topic}`
- Contract timeout: {timeout_ms} ms

## Important

Do not implement any business logic. All processing runs in the OmniMarket
`{node_name}` node. These instructions only cover argument mapping, node
dispatch, and output formatting.

Do not add concrete LLM provider names, served model IDs, endpoint URLs, or
fallback model defaults to this skill. If the backing node requires LLM work,
pass logical routing needs only when those fields are declared in the node
contract; runtime model resolution belongs to `node_model_router` and
contract/overlay policy.
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
    contract_inputs = (
        contract.get("inputs") if isinstance(contract.get("inputs"), dict) else {}
    )
    entry_flags: dict[str, str] = metadata.get("entry_flags") or _derive_entry_flags(
        contract_inputs
    )
    tags: list[str] = metadata.get("tags") or []
    node_alias = str(contract.get("name", "")).strip() or node_name

    command_topic = _get_command_topic(contract)
    completion_topic = _get_completion_topic(contract)
    timeout_ms = _get_timeout_ms(contract)
    contract_command_name = str(contract.get("name", "")).strip() or node_name

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
    instructions_kwargs = {**shared_kwargs, "node_alias": node_alias}
    instructions_kwargs["contract_command_name"] = contract_command_name

    skill_content = _render_skill_md(
        pack=pack,
        tags=tags,
        **shared_kwargs,
    )
    mdc_content = _render_mdc(**shared_kwargs)
    instructions_content = _render_instructions_md(**instructions_kwargs)

    claude_dir = output_dir / "claude_code"
    cursor_dir = output_dir / "cursor"
    codex_dir = output_dir / "codex"

    skill_path = claude_dir / f"{slug}_SKILL.md"
    mdc_path = cursor_dir / f"{slug}.mdc"
    instructions_path = codex_dir / "skills" / slug / "SKILL.md"

    if not dry_run:
        claude_dir.mkdir(parents=True, exist_ok=True)
        cursor_dir.mkdir(parents=True, exist_ok=True)
        codex_dir.mkdir(parents=True, exist_ok=True)

        skill_path.write_text(skill_content)
        mdc_path.write_text(mdc_content)
        instructions_path.parent.mkdir(parents=True, exist_ok=True)
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
    parser.add_argument(
        "--canonical-codex-skills",
        action="store_true",
        help="Generate/check the exact manifest-backed Codex skill paths only",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when canonical Codex skill output would change",
    )
    args = parser.parse_args(argv)

    if args.canonical_codex_skills:
        output_dir = (
            Path(args.output_dir) / "codex" / "skills"
            if args.output_dir == str(ADAPTERS_DIR)
            else Path(args.output_dir)
        )
        try:
            paths = generate_canonical_codex_skills(
                output_dir,
                dry_run=args.dry_run,
                check=args.check,
            )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            print(f"Canonical Codex skill generation failed: {exc}", file=sys.stderr)
            return 1
        prefix = "[DRY RUN] " if args.dry_run else ""
        action = "Checked" if args.check else "Generated"
        print(
            f"{prefix}{action} {len(paths)} canonical Codex skill paths in {output_dir}"
        )
        return 0

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
