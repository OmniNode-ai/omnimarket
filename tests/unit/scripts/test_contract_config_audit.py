"""Unit tests for the contract config audit script (OMN-10565 / Task 17)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from scripts.audit import contract_config_audit as audit


def _write_contract(node_dir: Path, body: str) -> None:
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "contract.yaml").write_text(body, encoding="utf-8")


def _write_handler(node_dir: Path, name: str, body: str) -> None:
    handlers = node_dir / "handlers"
    handlers.mkdir(parents=True, exist_ok=True)
    (handlers / f"{name}.py").write_text(body, encoding="utf-8")


@pytest.fixture
def nodes_dir(tmp_path: Path) -> Path:
    root = tmp_path / "nodes"
    root.mkdir()
    return root


def test_classifies_pure_compute_as_config_free(nodes_dir: Path) -> None:
    node = nodes_dir / "node_pure_compute"
    _write_contract(
        node,
        """---
name: pure_compute
node_type: compute
""",
    )
    _write_handler(node, "handler_pure", "def run(): return 1\n")
    rows = [audit.audit_contract(p) for p in sorted(nodes_dir.glob("*/contract.yaml"))]
    assert rows[0] is not None
    assert rows[0].classification == "config_free"


def test_classifies_kafka_publisher_as_config_required(nodes_dir: Path) -> None:
    node = nodes_dir / "node_kafka_pub"
    _write_contract(
        node,
        """---
name: kafka_pub
node_type: effect
event_bus:
  publish_topics:
    - "onex.evt.omnimarket.thing-happened.v1"
""",
    )
    rows = [audit.audit_contract(p) for p in sorted(nodes_dir.glob("*/contract.yaml"))]
    row = rows[0]
    assert row is not None
    assert row.classification == "config_required"
    assert row.publish_topics_count == 1


def test_classifies_config_block_as_config_required(nodes_dir: Path) -> None:
    node = nodes_dir / "node_with_config"
    _write_contract(
        node,
        """---
name: with_config
node_type: effect
config:
  llm_url:
    env_var: LLM_CODER_URL
    required: true
""",
    )
    rows = [audit.audit_contract(p) for p in sorted(nodes_dir.glob("*/contract.yaml"))]
    row = rows[0]
    assert row is not None
    assert row.classification == "config_required"
    assert "LLM_CODER_URL" in row.declared_config_env_vars


def test_dependency_name_inference_marks_config_required(nodes_dir: Path) -> None:
    """Most contracts use dependency_type=service and encode transport in name."""
    node = nodes_dir / "node_deps_only"
    _write_contract(
        node,
        """---
name: deps_only
node_type: orchestrator
dependencies:
  - name: HandlerKafka
    dependency_type: service
    required: true
  - name: AdapterValkey
    dependency_type: service
    required: true
""",
    )
    rows = [audit.audit_contract(p) for p in sorted(nodes_dir.glob("*/contract.yaml"))]
    row = rows[0]
    assert row is not None
    assert row.classification == "config_required"
    assert "kafka" in row.classification_reason
    assert "valkey" in row.classification_reason


def test_handler_uses_postgres_but_contract_silent_is_needs_review(
    nodes_dir: Path,
) -> None:
    node = nodes_dir / "node_drift"
    _write_contract(
        node,
        """---
name: drift
node_type: effect
""",
    )
    _write_handler(
        node,
        "handler_drift",
        "import asyncpg\n\nasync def run(pool: asyncpg.Pool) -> None:\n    pass\n",
    )
    rows = [audit.audit_contract(p) for p in sorted(nodes_dir.glob("*/contract.yaml"))]
    row = rows[0]
    assert row is not None
    assert row.classification == "needs_review"
    assert "postgres" in row.handler_transports_detected


def test_handler_kafka_with_topics_is_not_drift(nodes_dir: Path) -> None:
    """kafka usage in handler is not drift if event_bus declares topics."""
    node = nodes_dir / "node_kafka_aligned"
    _write_contract(
        node,
        """---
name: kafka_aligned
node_type: effect
event_bus:
  publish_topics:
    - "onex.evt.omnimarket.x.v1"
""",
    )
    _write_handler(
        node,
        "handler_aligned",
        "from aiokafka import AIOKafkaProducer\n\nasync def run(): pass\n",
    )
    rows = [audit.audit_contract(p) for p in sorted(nodes_dir.glob("*/contract.yaml"))]
    row = rows[0]
    assert row is not None
    assert row.classification == "config_required"


def test_main_writes_csv_and_md(nodes_dir: Path, tmp_path: Path) -> None:
    node = nodes_dir / "node_pure"
    _write_contract(node, "---\nname: pure\nnode_type: compute\n")
    csv_path = tmp_path / "out.csv"
    md_path = tmp_path / "out.md"

    rc = audit.main(
        [
            "--nodes-dir",
            str(nodes_dir),
            "--csv-out",
            str(csv_path),
            "--md-out",
            str(md_path),
        ]
    )
    assert rc == 0

    with csv_path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["node_name"] == "node_pure"
    assert rows[0]["classification"] in {
        "config_free",
        "config_required",
        "needs_review",
    }

    md_text = md_path.read_text(encoding="utf-8")
    assert "Counts by classification" in md_text


def test_repo_audit_runs_and_produces_one_row_per_contract() -> None:
    """Smoke test: run against the real omnimarket nodes/ tree."""
    nodes_dir = Path(audit.NODES_DIR)
    contract_paths = sorted(nodes_dir.glob("*/contract.yaml"))
    assert len(contract_paths) > 0, "expected omnimarket nodes/ tree to be present"

    rows = []
    for cp in contract_paths:
        r = audit.audit_contract(cp)
        if r is not None:
            rows.append(r)

    assert len(rows) == len(contract_paths)
    classifications = {r.classification for r in rows}
    assert classifications.issubset({"config_free", "config_required", "needs_review"})
