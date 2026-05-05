"""Contract classification regression test (OMN-10567 / Task 18).

Verifies the per-plan acceptance: every node not classified as `config_free`
in the audit (i.e. every `config_required` node) has `metadata.transport_type`
set in its contract.yaml. `needs_review` nodes are deliberately exempt — they
are flagged for human follow-up, not auto-edited.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
NODES_DIR = REPO_ROOT / "src" / "omnimarket" / "nodes"
AUDIT_CSV = REPO_ROOT / "docs" / "audits" / "2026-05-05-contract-config-audit.csv"


def _read_audit() -> list[dict[str, str]]:
    with AUDIT_CSV.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _read_contract(node_name: str) -> dict:
    path = NODES_DIR / node_name / "contract.yaml"
    if not path.exists():
        raise FileNotFoundError(f"missing contract.yaml for {node_name}")
    text = path.read_text(encoding="utf-8")
    docs = list(yaml.safe_load_all(text))
    real = [d for d in docs if isinstance(d, dict)]
    if not real:
        raise ValueError(f"empty YAML in {path}")
    if len(real) > 1:
        merged: dict = {}
        for d in real:
            merged.update(d)
        return merged
    return real[0]


@pytest.fixture(scope="module")
def audit_rows() -> list[dict[str, str]]:
    return _read_audit()


def test_audit_csv_present() -> None:
    assert AUDIT_CSV.exists(), (
        f"Task 17 audit CSV missing: {AUDIT_CSV} — Task 18 cannot validate without it"
    )


def test_every_config_required_node_has_transport_type(
    audit_rows: list[dict[str, str]],
) -> None:
    missing: list[str] = []
    for row in audit_rows:
        if row["classification"] != "config_required":
            continue
        contract = _read_contract(row["node_name"])
        md = contract.get("metadata")
        if not isinstance(md, dict) or not md.get("transport_type"):
            missing.append(row["node_name"])

    assert not missing, (
        f"{len(missing)} config_required nodes have no metadata.transport_type: {missing[:10]}"
    )


def test_config_free_and_needs_review_are_exempt(
    audit_rows: list[dict[str, str]],
) -> None:
    """Sanity: classifications other than config_required are not asserted on.

    This documents that needs_review (10 rows in current audit) is flagged for
    human follow-up — Task 18 does NOT auto-edit them.
    """
    classifications = {r["classification"] for r in audit_rows}
    assert classifications.issubset({"config_free", "config_required", "needs_review"})

    needs_review = [
        r["node_name"] for r in audit_rows if r["classification"] == "needs_review"
    ]
    assert needs_review, "expected at least one needs_review row from Task 17 audit"


def test_transport_type_value_is_known(audit_rows: list[dict[str, str]]) -> None:
    """transport_type must be one of the canonical transports the apply script
    can choose. Catches stray hand-edits with unknown values."""
    allowed = {"kafka", "postgres", "valkey", "infisical", "llm"}
    for row in audit_rows:
        if row["classification"] != "config_required":
            continue
        contract = _read_contract(row["node_name"])
        md = contract.get("metadata", {})
        if not isinstance(md, dict):
            continue
        value = md.get("transport_type")
        if value is None:
            continue
        assert value in allowed, (
            f"{row['node_name']}: metadata.transport_type={value!r} not in {sorted(allowed)}"
        )


def test_apply_script_idempotent_on_clean_tree() -> None:
    """Running the apply script after a clean apply must produce zero changes."""
    import subprocess

    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/audit/apply_contract_config.py",
            "--summary",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"apply script failed: {result.stderr}"
    assert "changed=0" in result.stdout, (
        f"apply script not idempotent: stdout={result.stdout}"
    )
