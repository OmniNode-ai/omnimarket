# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``classify_head_checks`` seam on the triage compute (OMN-19825).

Wave 1 of the PR landing workflow froze this operation's contract and
models; the classifier itself is OMN-19830 and is tested in
``test_classify_head_checks.py``. These tests hold the seam to its three
acceptance criteria:

* AC1: the 2026-09-26 corpus covers every verdict with at least three real
  heads, and every fixture cites its repository, PR and head sha.
* AC2: the operation's contract names the input and output models, and
  both validate every corpus input and expected output, round trip.
* AC3: the verdict model refuses a ``product_failed`` verdict that also
  names a re-runnable check.

They also pin that the operation is not wired on the runtime: the contract
declares it, and runtime discovery routes no message to it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import (
    discover_contracts_from_paths,
)
from pydantic import ValidationError

import omnimarket.nodes.node_pr_lifecycle_triage_compute as node_pkg
from omnimarket.merge_control.reason_code_classifier import (
    EnumMergeCheckReasonCode,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_head_checks import (
    HandlerClassifyHeadChecks,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_verdict import (
    HEAD_CHECK_RERUN_VERDICTS,
    EnumHeadCheckVerdict,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_facts import (
    ModelHeadCheckFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_verdict import (
    ModelHeadCheckVerdict,
)

pytestmark = pytest.mark.unit

_NODE_DIR = Path(node_pkg.__file__).parent
_CONTRACT = _NODE_DIR / "contract.yaml"
_CORPUS = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "pr_landing"
    / "checks"
    / "2026-09-26"
)
_OPERATION = "classify_head_checks"
_SHA = "0123456789abcdef0123456789abcdef01234567"


def _corpus() -> list[tuple[Path, dict[str, Any]]]:
    files = sorted(_CORPUS.glob("*.json"))
    return [(path, json.loads(path.read_text())) for path in files]


def _manifest() -> dict[str, Any]:
    data = json.loads(_CORPUS.with_name(f"{_CORPUS.name}.manifest.json").read_text())
    assert isinstance(data, dict)
    return data


def _operation_entry() -> dict[str, Any]:
    raw = yaml.safe_load(_CONTRACT.read_text())
    entries = [op for op in raw.get("operations", []) if op["name"] == _OPERATION]
    assert len(entries) == 1, f"{_OPERATION} must be declared exactly once"
    entry = entries[0]
    assert isinstance(entry, dict)
    return entry


# --------------------------------------------------------------------- AC1


def test_corpus_has_at_least_forty_real_heads() -> None:
    corpus = _corpus()
    heads = {
        (d["cites"]["repository"], d["cites"]["pr_number"], d["cites"]["head_sha"])
        for _, d in corpus
    }
    assert len(corpus) >= 40, f"corpus holds {len(corpus)} fixtures, needs 40"
    assert len(heads) == len(corpus), "two fixtures cite the same head"


def test_corpus_covers_every_verdict_with_three_heads() -> None:
    """AC1 coverage, with the day's measured gaps named rather than hidden.

    A verdict may hold fewer than three heads only when the corpus manifest
    records it with the measurement that found the day produced no more, and
    the recorded count must equal the corpus count, so a gap cannot be
    recorded for a verdict the corpus does cover, or drift silently.
    """
    counts = Counter(d["expected"]["verdict"] for _, d in _corpus())
    recorded = _manifest()["verdicts_short_of_three_heads"]
    short = {
        verdict.value: counts.get(verdict.value, 0)
        for verdict in EnumHeadCheckVerdict
        if counts.get(verdict.value, 0) < 3
    }
    assert set(short) == set(recorded), (
        f"verdicts with fewer than three heads: {short}; recorded as measured "
        f"gaps: {sorted(recorded)}"
    )
    for verdict, entry in recorded.items():
        assert entry["heads"] == short[verdict], verdict
        assert entry["measurement"].strip(), verdict


def test_manifest_precedence_orders_every_verdict_once() -> None:
    precedence = _manifest()["labelling_rules"]["precedence"]
    assert sorted(precedence) == sorted(v.value for v in EnumHeadCheckVerdict)
    assert len(precedence) == len(set(precedence))


def test_heads_from_other_dates_fill_only_the_verdicts_the_day_lacked() -> None:
    """The day had no timed_out or runner_infra head; only those come from other dates."""
    other_dates = _manifest()["heads_from_other_dates"]
    by_name = {path.name: data for path, data in _corpus()}
    for name in other_dates["fixtures"]:
        assert name in by_name, f"{name} is listed but not in the corpus"
        assert by_name[name]["expected"]["verdict"] in {"timed_out", "runner_infra"}, (
            name
        )


def test_every_fixture_cites_its_repository_pr_and_head_sha() -> None:
    for path, data in _corpus():
        cites = data["cites"]
        assert set(cites) == {"repository", "pr_number", "head_sha"}, path.name
        assert cites["repository"].startswith("OmniNode-ai/"), path.name
        assert isinstance(cites["pr_number"], int), path.name
        assert len(cites["head_sha"]) == 40, path.name
        for side in ("input", "expected"):
            for key in ("repository", "pr_number", "head_sha"):
                assert data[side][key] == cites[key], f"{path.name}: {side}.{key}"
        repo = cites["repository"].split("/", 1)[1]
        stem = f"{repo}__{cites['pr_number']}__{cites['head_sha'][:12]}"
        assert path.stem == stem, f"{path.name} should be named {stem}.json"


def test_every_fixture_carries_a_hand_label_and_capture_provenance() -> None:
    other_dates = _manifest()["heads_from_other_dates"]
    first, last = other_dates["window"]
    for path, data in _corpus():
        label = data["label"]
        assert label["assigned_by"] == "hand", path.name
        assert label["rationale"].strip(), path.name
        provenance = data["provenance"]
        assert provenance["captured_at"].startswith("2026-09-2"), path.name
        excluded = provenance["copies_started_after_observed_at"]
        assert (
            provenance["check_runs_total_count"]
            == len(data["input"]["checks"]) + excluded
        ), f"{path.name}: the check-run read was not complete"
        observed_day = data["input"]["observed_at"][:10]
        if path.name in other_dates["fixtures"]:
            assert first <= observed_day <= last, path.name
        else:
            assert observed_day == "2026-09-26", path.name
        if excluded or provenance["reconstructed"]:
            assert provenance["reconstruction_rule"].strip(), path.name


# --------------------------------------------------------------------- AC2


def test_contract_declares_the_operation_with_its_models() -> None:
    entry = _operation_entry()
    assert entry["input_model"]["name"] == "ModelHeadCheckFacts"
    assert entry["input_model"]["module"].endswith("models.model_head_check_facts")
    assert entry["output_model"]["name"] == "ModelHeadCheckVerdict"
    assert entry["output_model"]["module"].endswith("models.model_head_check_verdict")
    assert entry["handler"]["name"] == "HandlerClassifyHeadChecks"
    assert entry["handler"]["module"].endswith("handlers.handler_classify_head_checks")


def test_contract_model_refs_resolve_to_the_seam_classes() -> None:
    import importlib

    entry = _operation_entry()
    for ref, cls in (
        (entry["input_model"], ModelHeadCheckFacts),
        (entry["output_model"], ModelHeadCheckVerdict),
        (entry["handler"], HandlerClassifyHeadChecks),
    ):
        module = importlib.import_module(ref["module"])
        assert getattr(module, ref["name"]) is cls


def test_every_corpus_input_and_expected_output_round_trips() -> None:
    corpus = _corpus()
    assert corpus, "empty corpus proves nothing"
    for path, data in corpus:
        facts = ModelHeadCheckFacts.model_validate(data["input"])
        verdict = ModelHeadCheckVerdict.model_validate(data["expected"])
        assert facts.model_dump(mode="json") == data["input"], path.name
        assert verdict.model_dump(mode="json") == data["expected"], path.name
        assert ModelHeadCheckFacts.model_validate_json(facts.model_dump_json()) == facts
        assert (
            ModelHeadCheckVerdict.model_validate_json(verdict.model_dump_json())
            == verdict
        )


def test_per_check_reason_codes_reuse_the_merge_check_classifier_enum() -> None:
    seen = 0
    for _, data in _corpus():
        verdict = ModelHeadCheckVerdict.model_validate(data["expected"])
        for reason in verdict.check_reasons:
            assert isinstance(reason.reason_code, EnumMergeCheckReasonCode)
            seen += 1
    assert seen, "no fixture carries a per-check reason code"


def test_facts_refuse_an_unknown_field() -> None:
    _, data = _corpus()[0]
    bad = dict(data["input"], rollup_state="SUCCESS")
    with pytest.raises(ValidationError):
        ModelHeadCheckFacts.model_validate(bad)


def test_facts_refuse_a_completed_check_without_a_conclusion() -> None:
    _, data = _corpus()[0]
    bad = json.loads(json.dumps(data["input"]))
    check = bad["checks"][0]
    check["status"] = "completed"
    check["conclusion"] = None
    with pytest.raises(ValidationError, match="conclusion"):
        ModelHeadCheckFacts.model_validate(bad)


def test_facts_refuse_a_merged_companion_without_its_pr() -> None:
    _, data = _corpus()[0]
    bad = dict(data["input"], companion_state="merged", companion_pr=None)
    with pytest.raises(ValidationError, match="companion_pr"):
        ModelHeadCheckFacts.model_validate(bad)


# --------------------------------------------------------------------- AC3


def _verdict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "repository": "OmniNode-ai/omnimarket",
        "pr_number": 1,
        "head_sha": _SHA,
        "verdict": "product_failed",
        "rerun_checks": [],
        "check_reasons": [],
    }
    base.update(overrides)
    return base


def test_product_failed_naming_a_rerunnable_check_is_refused() -> None:
    with pytest.raises(ValidationError, match="product_failed"):
        ModelHeadCheckVerdict.model_validate(
            _verdict(verdict="product_failed", rerun_checks=["CI Summary"])
        )


def test_product_failed_without_a_rerun_is_accepted() -> None:
    verdict = ModelHeadCheckVerdict.model_validate(_verdict())
    assert verdict.verdict is EnumHeadCheckVerdict.PRODUCT_FAILED
    assert verdict.rerun_checks == ()


@pytest.mark.parametrize("verdict", sorted(v.value for v in HEAD_CHECK_RERUN_VERDICTS))
def test_a_rerun_verdict_must_name_what_to_rerun(verdict: str) -> None:
    with pytest.raises(ValidationError, match="rerun_checks"):
        ModelHeadCheckVerdict.model_validate(_verdict(verdict=verdict))
    accepted = ModelHeadCheckVerdict.model_validate(
        _verdict(verdict=verdict, rerun_checks=["CI Summary"])
    )
    assert accepted.rerun_checks == ("CI Summary",)


@pytest.mark.parametrize(
    "verdict",
    sorted(
        v.value
        for v in EnumHeadCheckVerdict
        if v not in HEAD_CHECK_RERUN_VERDICTS
        and v is not EnumHeadCheckVerdict.PRODUCT_FAILED
    ),
)
def test_a_non_rerun_verdict_refuses_a_rerun(verdict: str) -> None:
    with pytest.raises(ValidationError, match="rerun_checks"):
        ModelHeadCheckVerdict.model_validate(
            _verdict(verdict=verdict, rerun_checks=["CI Summary"])
        )


def test_stale_caller_pin_is_not_a_rerun_verdict() -> None:
    assert EnumHeadCheckVerdict.STALE_CALLER_PIN not in HEAD_CHECK_RERUN_VERDICTS
    assert EnumHeadCheckVerdict.BEHIND_REQUIRED not in HEAD_CHECK_RERUN_VERDICTS


def test_rerun_checks_refuse_duplicates() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        ModelHeadCheckVerdict.model_validate(
            _verdict(verdict="timed_out", rerun_checks=["CI Summary", "CI Summary"])
        )


# ---------------------------------------------------------- runtime wiring


def test_runtime_discovery_routes_nothing_to_the_new_operation() -> None:
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors, manifest.errors
    assert len(manifest.contracts) == 1
    contract = manifest.contracts[0]
    routing = contract.handler_routing
    assert routing is not None
    operations = [entry.operation for entry in routing.handlers]
    handler_names = [entry.handler.name for entry in routing.handlers]
    assert operations == ["triage_prs"]
    assert "HandlerClassifyHeadChecks" not in handler_names
    assert contract.event_bus is not None
    assert list(contract.event_bus.subscribe_topics) == [
        "onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1"
    ]
