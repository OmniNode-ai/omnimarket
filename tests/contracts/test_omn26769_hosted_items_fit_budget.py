"""The OMN-26769 hosted evidence items fit the hosted per-check budget.

repo-evidence / dod-verify kills any hosted check at DOD_VERIFY_CHECK_TIMEOUT_S
(300 s) and records it unverifiable, which refuses the pull request. The whole
tests/nodes/test_work_contract_checks.py module runs longer than that, so it
belongs only to the unbound local_done_gate item (run on a lab host); the hosted
AC2 item runs the corpus-load test that states the AC2 claim.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-26769.yaml"
_WHOLE_MODULE = "tests/nodes/test_work_contract_checks.py"


def _items() -> dict[str, dict]:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return {item["id"]: item for item in document["dod_evidence"]}


def test_hosted_items_never_run_the_whole_checks_module() -> None:
    for item_id, item in _items().items():
        if item.get("execution_scope") == "local_done_gate":
            continue
        for check in item["checks"]:
            assert _WHOLE_MODULE not in check["check_value"], item_id


def test_the_whole_checks_module_still_runs_on_the_lab_item() -> None:
    lab = _items()["dod-omn26769-integration-proof"]
    assert lab["execution_scope"] == "local_done_gate"
    assert _WHOLE_MODULE in lab["checks"][0]["check_value"]


def test_ac2_is_still_bound_to_a_hosted_corpus_load_check() -> None:
    ac2 = _items()["dod-omn26769-ac2"]
    assert ac2["binds_ac"] == ["AC2"]
    assert (
        "test_work_contract_corpus_pins_match_tree" in ac2["checks"][0]["check_value"]
    )
