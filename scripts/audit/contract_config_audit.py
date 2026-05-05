"""Contract config audit for omnimarket nodes (OMN-10565 / Task 17 / Epic 4).

Walks every `src/omnimarket/nodes/*/contract.yaml`, parses it, cross-references
the node's handler `*.py` files for actual transport/SDK usage, and emits a CSV
+ summary markdown classifying each contract:

  config_free    — node has no transport/secret/env-var config requirements
  config_required — node uses Kafka / Postgres / Valkey / Infisical / LLM /
                    declares `config:` block with env_var entries, or imports
                    transport handlers in its handler modules
  needs_review   — handler imports indicate transport usage but contract
                   `dependencies[]` does not declare it (drift) — flagged for
                   human follow-up in Task 18

This is the input for Task 18 (apply scoped contract changes) — it deliberately
does NOT modify any contract. ChatGPT review §8: no blanket-edit of all 151
contracts.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NODES_DIR = REPO_ROOT / "src" / "omnimarket" / "nodes"
DEFAULT_CSV_OUT = REPO_ROOT / "docs" / "audits" / "2026-05-05-contract-config-audit.csv"
DEFAULT_MD_OUT = REPO_ROOT / "docs" / "audits" / "2026-05-05-contract-config-audit.md"

# Handler-import patterns that indicate the node consumes a given transport.
# Each entry: (transport_label, regex). Detection is grep-based across all
# *.py under <node>/ (handlers + models + consumers + probes).
TRANSPORT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "kafka",
        re.compile(
            r"\b(aiokafka|KafkaConsumer|KafkaProducer|HandlerKafka|AdapterKafka)\b"
        ),
    ),
    (
        "postgres",
        re.compile(
            r"\b(asyncpg|psycopg|HandlerPostgres|AdapterPostgres|HandlerDb|AdapterDb)\b"
        ),
    ),
    (
        "valkey",
        re.compile(
            r"\b(AdapterValkey|HandlerValkey|valkey|redis\.asyncio|redis\.Redis)\b"
        ),
    ),
    (
        "infisical",
        re.compile(r"\b(HandlerInfisical|AdapterInfisical|InfisicalSecretStore)\b"),
    ),
    (
        "llm",
        re.compile(
            r"\b(LLM_CODER|LLM_REASONER|LLM_FALLBACK|LLM_DEEPSEEK|HandlerLLM|RouterLLM|AdapterLLM|bridge_config_loader)\b"
        ),
    ),
]

DEPENDENCY_TYPE_TO_TRANSPORT = {
    "kafka": "kafka",
    "postgres": "postgres",
    "database": "postgres",
    "db": "postgres",
    "valkey": "valkey",
    "redis": "valkey",
    "infisical": "infisical",
    "secret_store": "infisical",
    "llm": "llm",
}

# Most contracts use generic dependency_type values (`service`, `protocol`,
# `repository`) and encode the transport identity in the `name` field. Match
# the name against these patterns to infer the transport.
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


@dataclass
class ContractRow:
    node_name: str
    node_type: str
    has_metadata_transport_type: bool
    metadata_transport_type: str
    declared_dependencies: list[str] = field(default_factory=list)
    declared_config_env_vars: list[str] = field(default_factory=list)
    publish_topics_count: int = 0
    subscribe_topics_count: int = 0
    handler_transports_detected: list[str] = field(default_factory=list)
    classification: str = ""
    classification_reason: str = ""

    def to_csv_row(self) -> dict[str, str]:
        return {
            "node_name": self.node_name,
            "node_type": self.node_type,
            "has_metadata_transport_type": str(
                self.has_metadata_transport_type
            ).lower(),
            "metadata_transport_type": self.metadata_transport_type,
            "declared_dependencies": "|".join(sorted(self.declared_dependencies)),
            "declared_config_env_vars": "|".join(sorted(self.declared_config_env_vars)),
            "publish_topics_count": str(self.publish_topics_count),
            "subscribe_topics_count": str(self.subscribe_topics_count),
            "handler_transports_detected": "|".join(
                sorted(self.handler_transports_detected)
            ),
            "classification": self.classification,
            "classification_reason": self.classification_reason,
        }


def _load_contract(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    docs = list(yaml.safe_load_all(text))
    # Filter out None docs from leading `---` separators.
    real = [d for d in docs if isinstance(d, dict)]
    if not real:
        return {}
    if len(real) > 1:
        merged: dict = {}
        for d in real:
            merged.update(d)
        return merged
    return real[0]


def _string_list(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _extract_dependencies(contract: dict) -> tuple[list[str], list[str], set[str]]:
    """Return (dependency_names, dependency_types_lowercase, inferred_transports)."""
    deps = contract.get("dependencies") or []
    names: list[str] = []
    types: list[str] = []
    inferred: set[str] = set()
    if isinstance(deps, list):
        for d in deps:
            if isinstance(d, dict):
                name = d.get("name")
                dtype = d.get("dependency_type") or d.get("type") or ""
                if name:
                    names.append(str(name))
                    for transport, pattern in DEPENDENCY_NAME_PATTERNS:
                        if pattern.search(str(name)):
                            inferred.add(transport)
                if dtype:
                    types.append(str(dtype).strip().lower())
            elif isinstance(d, str):
                names.append(d)
                for transport, pattern in DEPENDENCY_NAME_PATTERNS:
                    if pattern.search(d):
                        inferred.add(transport)
    return names, types, inferred


def _extract_config_env_vars(contract: dict) -> list[str]:
    cfg = contract.get("config") or {}
    keys: list[str] = []
    if isinstance(cfg, dict):
        for entry in cfg.values():
            if isinstance(entry, dict):
                ev = entry.get("env_var")
                if ev:
                    keys.append(str(ev))
    return sorted(set(keys))


def _extract_metadata_transport(contract: dict) -> tuple[bool, str]:
    md = contract.get("metadata") or {}
    if not isinstance(md, dict):
        return False, ""
    val = md.get("transport_type")
    if not val:
        return False, ""
    return True, str(val)


def _extract_topics(contract: dict) -> tuple[int, int]:
    bus = contract.get("event_bus") or {}
    if not isinstance(bus, dict):
        return 0, 0
    pubs = bus.get("publish_topics") or []
    subs = bus.get("subscribe_topics") or []
    pub_n = len(pubs) if isinstance(pubs, list) else 0
    sub_n = len(subs) if isinstance(subs, list) else 0
    return pub_n, sub_n


def _detect_handler_transports(node_dir: Path) -> list[str]:
    detected: set[str] = set()
    for py in node_dir.rglob("*.py"):
        # Skip cached / generated files and tests.
        rel = py.relative_to(node_dir)
        if rel.parts and rel.parts[0] in {"__pycache__", "tests", "test"}:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except OSError:
            continue
        for label, pattern in TRANSPORT_PATTERNS:
            if pattern.search(text):
                detected.add(label)
    return sorted(detected)


def _declared_transports(dep_types: Iterable[str], inferred: Iterable[str]) -> set[str]:
    declared: set[str] = set(inferred)
    for t in dep_types:
        mapped = DEPENDENCY_TYPE_TO_TRANSPORT.get(t)
        if mapped:
            declared.add(mapped)
    return declared


def _classify(
    row: ContractRow, dep_types: list[str], inferred_transports: set[str]
) -> tuple[str, str]:
    """Return (classification, reason)."""
    handler_set = set(row.handler_transports_detected)
    declared_set = _declared_transports(dep_types, inferred_transports)
    has_topics = (row.publish_topics_count + row.subscribe_topics_count) > 0
    has_config_block = bool(row.declared_config_env_vars)

    # If the contract declares `config:` env vars or has event-bus topics or
    # any transport in handlers, it's config_required.
    config_signals = (
        bool(handler_set) or has_topics or has_config_block or bool(declared_set)
    )

    if not config_signals:
        return (
            "config_free",
            "no handler transports, no config env_vars, no topics, no transport deps",
        )

    # Drift detection: handler imports a transport not reflected in
    # dependencies[] or topics.
    undeclared = handler_set - declared_set
    if has_topics:
        undeclared.discard("kafka")  # topics imply kafka

    if undeclared:
        return (
            "needs_review",
            f"handler uses {sorted(undeclared)} but contract dependencies[] does not declare it",
        )

    return "config_required", (
        f"handler_transports={sorted(handler_set)}, "
        f"declared_dep_transports={sorted(declared_set)}, "
        f"config_env_vars={len(row.declared_config_env_vars)}, "
        f"publish={row.publish_topics_count}, subscribe={row.subscribe_topics_count}"
    )


def audit_contract(contract_path: Path) -> ContractRow | None:
    node_dir = contract_path.parent
    node_name = node_dir.name
    contract = _load_contract(contract_path)
    if not contract:
        return None

    has_md, md_value = _extract_metadata_transport(contract)
    dep_names, dep_types, inferred_transports = _extract_dependencies(contract)
    cfg_keys = _extract_config_env_vars(contract)
    pub_n, sub_n = _extract_topics(contract)

    row = ContractRow(
        node_name=node_name,
        node_type=str(contract.get("node_type") or "").strip(),
        has_metadata_transport_type=has_md,
        metadata_transport_type=md_value,
        declared_dependencies=dep_names,
        declared_config_env_vars=cfg_keys,
        publish_topics_count=pub_n,
        subscribe_topics_count=sub_n,
        handler_transports_detected=_detect_handler_transports(node_dir),
    )
    classification, reason = _classify(row, dep_types, inferred_transports)
    row.classification = classification
    row.classification_reason = reason
    return row


def write_csv(rows: list[ContractRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
        list(rows[0].to_csv_row().keys())
        if rows
        else [
            "node_name",
            "node_type",
            "has_metadata_transport_type",
            "metadata_transport_type",
            "declared_dependencies",
            "declared_config_env_vars",
            "publish_topics_count",
            "subscribe_topics_count",
            "handler_transports_detected",
            "classification",
            "classification_reason",
        ]
    )
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in sorted(rows, key=lambda r: r.node_name):
            writer.writerow(r.to_csv_row())


def write_summary_md(rows: list[ContractRow], path: Path, csv_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {"config_free": 0, "config_required": 0, "needs_review": 0}
    for r in rows:
        counts[r.classification] = counts.get(r.classification, 0) + 1
    transport_tally: dict[str, int] = {}
    for r in rows:
        for t in r.handler_transports_detected:
            transport_tally[t] = transport_tally.get(t, 0) + 1
    needs_review_rows = sorted(
        (r for r in rows if r.classification == "needs_review"),
        key=lambda r: r.node_name,
    )

    lines: list[str] = []
    lines.append("# Contract Config Audit Summary — 2026-05-05")
    lines.append("")
    lines.append("**Ticket:** OMN-10565 (Task 17, Epic 4: Contract-Declared Config)")
    lines.append("")
    try:
        csv_display = csv_path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        csv_display = csv_path.as_posix()
    lines.append(f"**Source CSV:** `{csv_display}`")
    lines.append("")
    lines.append("## Counts by classification")
    lines.append("")
    lines.append("| classification | count |")
    lines.append("|---|---|")
    for k in ("config_free", "config_required", "needs_review"):
        lines.append(f"| {k} | {counts.get(k, 0)} |")
    lines.append(f"| **total** | **{len(rows)}** |")
    lines.append("")
    lines.append("## Handler transport tally (across all rows)")
    lines.append("")
    lines.append("| transport | nodes |")
    lines.append("|---|---|")
    for t in sorted(transport_tally):
        lines.append(f"| {t} | {transport_tally[t]} |")
    lines.append("")
    lines.append("## needs_review nodes")
    lines.append("")
    lines.append(
        "These nodes have handler imports that suggest transport usage not declared in "
        "`dependencies[]`. Task 18 will leave these flagged for human follow-up rather than "
        "auto-editing them."
    )
    lines.append("")
    if needs_review_rows:
        lines.append("| node_name | handler_transports | reason |")
        lines.append("|---|---|---|")
        for r in needs_review_rows:
            lines.append(
                f"| `{r.node_name}` | {','.join(r.handler_transports_detected)} | "
                f"{r.classification_reason} |"
            )
    else:
        lines.append("_None._")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--nodes-dir",
        type=Path,
        default=NODES_DIR,
        help="Directory containing node_*/contract.yaml folders",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=DEFAULT_CSV_OUT,
        help="Output CSV path",
    )
    parser.add_argument(
        "--md-out",
        type=Path,
        default=DEFAULT_MD_OUT,
        help="Output summary markdown path",
    )
    parser.add_argument(
        "--print-summary",
        action="store_true",
        help="Print per-classification counts to stdout",
    )
    args = parser.parse_args(argv)

    contract_paths = sorted(args.nodes_dir.glob("*/contract.yaml"))
    if not contract_paths:
        print(
            f"ERROR: no contract.yaml files found under {args.nodes_dir}",
            file=sys.stderr,
        )
        return 2

    rows: list[ContractRow] = []
    for cp in contract_paths:
        try:
            row = audit_contract(cp)
        except yaml.YAMLError as exc:
            print(f"WARN: yaml parse error in {cp}: {exc}", file=sys.stderr)
            continue
        if row is not None:
            rows.append(row)

    write_csv(rows, args.csv_out)
    write_summary_md(rows, args.md_out, args.csv_out)

    if args.print_summary:
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.classification] = counts.get(r.classification, 0) + 1
        print(f"contracts_audited={len(rows)}")
        for k in sorted(counts):
            print(f"{k}={counts[k]}")
        print(f"csv={args.csv_out}")
        print(f"md={args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
