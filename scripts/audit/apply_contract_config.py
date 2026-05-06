"""Apply audited contract changes (OMN-10567 / Task 18 / Epic 4).

Reads the audit CSV emitted by `scripts/audit/contract_config_audit.py` and,
for every row classified as `config_required`, ensures the contract YAML has
`metadata.transport_type` set to the actual dominant transport. Skips
`config_free` (no signal) and `needs_review` (drift between handler and
contract — flagged for human review per plan ChatGPT review §8).

Idempotent: re-running on a contract that already has the correct
`metadata.transport_type` is a no-op.

The dominant transport is chosen by this priority:
  1. Explicit handler import (kafka > postgres > valkey > infisical > llm)
  2. Inferred from dependencies[].name (same priority order)
  3. Kafka if event_bus has any topics
  4. Empty (not classified — should be impossible for config_required)
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NODES_DIR = REPO_ROOT / "src" / "omnimarket" / "nodes"
DEFAULT_CSV = REPO_ROOT / "docs" / "audits" / "2026-05-05-contract-config-audit.csv"

# Priority order — first transport that matches wins.
TRANSPORT_PRIORITY = ("kafka", "postgres", "valkey", "infisical", "llm")

# Same dependency name regex as the audit; kept here to keep apply self-contained.
DEPENDENCY_NAME_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("kafka", re.compile(r"Kafka", re.IGNORECASE)),
    (
        "postgres",
        re.compile(
            r"(Postgres|HandlerDb|AdapterDb|Database|repository)", re.IGNORECASE
        ),
    ),
    ("valkey", re.compile(r"(Valkey|Redis)", re.IGNORECASE)),
    ("infisical", re.compile(r"Infisical|SecretStore", re.IGNORECASE)),
    (
        "llm",
        re.compile(
            r"(\bLLM\b|llm_(coder|reasoner|fallback)|OpenAI|Anthropic|RouterLLM)",
            re.IGNORECASE,
        ),
    ),
]


def _read_audit_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _pick_dominant_transport(row: dict[str, str], contract: dict) -> str:
    """Choose a single transport for metadata.transport_type."""
    handler_str = row.get("handler_transports_detected") or ""
    handler_set = {t for t in handler_str.split("|") if t}
    for t in TRANSPORT_PRIORITY:
        if t in handler_set:
            return t

    deps = contract.get("dependencies") or []
    inferred: set[str] = set()
    if isinstance(deps, list):
        for d in deps:
            name = d.get("name") if isinstance(d, dict) else d
            if not isinstance(name, str):
                continue
            for transport, pattern in DEPENDENCY_NAME_PATTERNS:
                if pattern.search(name):
                    inferred.add(transport)
    for t in TRANSPORT_PRIORITY:
        if t in inferred:
            return t

    pubs = int(row.get("publish_topics_count", "0") or "0")
    subs = int(row.get("subscribe_topics_count", "0") or "0")
    if pubs + subs > 0:
        return "kafka"

    return ""


def _read_contract(path: Path) -> tuple[dict, str]:
    """Return (parsed_contract, raw_text)."""
    text = path.read_text(encoding="utf-8")
    docs = list(yaml.safe_load_all(text))
    real = [d for d in docs if isinstance(d, dict)]
    if not real:
        return {}, text
    if len(real) > 1:
        merged: dict = {}
        for d in real:
            merged.update(d)
        return merged, text
    return real[0], text


def _set_metadata_transport_type(text: str, transport: str) -> str:
    """Insert / update metadata.transport_type in the raw YAML text.

    Strategy: parse to verify, then do a string-level edit so we preserve
    comments, blank lines, and field ordering of every other key. Round-trip
    via yaml.safe_dump would erase all of that.
    """
    lines = text.splitlines(keepends=True)

    # Find any existing metadata: block at column 0.
    metadata_idx: int | None = None
    for idx, line in enumerate(lines):
        if line.startswith("metadata:"):
            metadata_idx = idx
            break

    new_line = f"  transport_type: {transport}\n"

    if metadata_idx is None:
        # No metadata block yet — append a new one at end of file.
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append("metadata:\n")
        lines.append(new_line)
        return "".join(lines)

    # Existing metadata block. Walk its body until we hit a non-indented line.
    body_start = metadata_idx + 1
    body_end = len(lines)
    for i in range(body_start, len(lines)):
        line = lines[i]
        stripped = line.lstrip()
        if not stripped:
            continue  # blank line inside block
        if not (line.startswith(" ") or line.startswith("\t")):
            body_end = i
            break

    # Look for an existing transport_type entry in the block.
    for i in range(body_start, body_end):
        if re.match(r"^\s+transport_type\s*:", lines[i]):
            current_value = lines[i].split(":", 1)[1].strip()
            if current_value == transport:
                return text  # already correct, idempotent
            # Replace the value on this line.
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            lines[i] = f"{indent}transport_type: {transport}\n"
            return "".join(lines)

    # No transport_type present — insert immediately after `metadata:` line.
    lines.insert(body_start, new_line)
    return "".join(lines)


def apply_to_contract(
    contract_path: Path, transport: str, dry_run: bool
) -> tuple[bool, str]:
    """Return (changed, reason)."""
    if not transport:
        return False, "no transport derivable"
    contract, text = _read_contract(contract_path)
    md = contract.get("metadata")
    if isinstance(md, dict) and md.get("transport_type") == transport:
        return False, "already set"
    new_text = _set_metadata_transport_type(text, transport)
    if new_text == text:
        return False, "no diff"
    if not dry_run:
        contract_path.write_text(new_text, encoding="utf-8")
    return True, f"set transport_type={transport}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help="Path to the audit CSV from contract_config_audit.py",
    )
    parser.add_argument(
        "--nodes-dir",
        type=Path,
        default=NODES_DIR,
        help="Directory containing node_*/contract.yaml folders",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print intended changes without writing",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print classification + change counts",
    )
    args = parser.parse_args(argv)

    if not args.csv.exists():
        print(f"ERROR: audit CSV not found: {args.csv}", file=sys.stderr)
        return 2

    rows = _read_audit_csv(args.csv)
    counters = {"changed": 0, "skipped_already_set": 0, "skipped_no_transport": 0}
    skipped_classifications: dict[str, int] = {}

    for row in rows:
        classification = row["classification"]
        if classification != "config_required":
            skipped_classifications[classification] = (
                skipped_classifications.get(classification, 0) + 1
            )
            continue

        node_name = row["node_name"]
        contract_path = args.nodes_dir / node_name / "contract.yaml"
        if not contract_path.exists():
            print(f"WARN: missing contract.yaml for {node_name}", file=sys.stderr)
            continue

        contract, _ = _read_contract(contract_path)
        transport = _pick_dominant_transport(row, contract)
        changed, reason = apply_to_contract(contract_path, transport, args.dry_run)
        if changed:
            counters["changed"] += 1
            if args.summary or args.dry_run:
                print(f"[CHANGE] {node_name}: {reason}")
        elif reason == "already set":
            counters["skipped_already_set"] += 1
        elif reason == "no transport derivable":
            counters["skipped_no_transport"] += 1
            print(f"[SKIP] {node_name}: no transport derivable", file=sys.stderr)

    if args.summary:
        print(f"changed={counters['changed']}")
        print(f"skipped_already_set={counters['skipped_already_set']}")
        print(f"skipped_no_transport={counters['skipped_no_transport']}")
        for cls, n in sorted(skipped_classifications.items()):
            print(f"skipped_{cls}={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
