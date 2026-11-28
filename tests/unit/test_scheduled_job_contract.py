# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26223: every scheduled job's input and output is read and written through a model.

A closeout sweep died on a scope bundle whose ``held`` was not an array: the producer and the
reader each checked the JSON by hand and shared no model. These cases pin the contract: the
producer validates before it writes, every reader parses with the same model and raises a typed
error naming the file and the field, and no reader hand-checks the fields any more.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_dod_closeout_sweep_compute import (
    runtime_dod_closeout_sweep as runtime,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers import (
    closer_scope_rules,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_scope_bundle import (
    ModelScopeBundle,
    ModelScopeCandidateRow,
    ModelScopePrStateRow,
    ModelScopeTextStateRow,
    ScopeContractError,
    parse_scope_bundle,
    parse_scope_rows,
)
from omnimarket.nodes.node_hourly_tick_effect.models.model_hourly_tick import (
    ModelSweepResult,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_record import (
    IntegrationRecordError,
    read_integration_record,
)
from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_approved_work_file import (
    ApprovedWorkFileError,
    parse_approved_work_file,
)
from omnimarket.nodes.node_linear_comments_decision_compute.models.model_held_draft import (
    HeldDraftListError,
    read_held_drafts,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures"
PARITY = (
    Path(__file__).parents[1]
    / "nodes"
    / "node_dod_closeout_sweep_compute"
    / "fixtures"
    / "scope_bundle_legacy_parity.json"
)
RUNTIME_MODULE = (
    "omnimarket.nodes.node_dod_closeout_sweep_compute.runtime_dod_closeout_sweep"
)


def _valid_bundle() -> dict[str, Any]:
    legacy: dict[str, Any] = json.loads(PARITY.read_text())["legacy_bundles"]["12"]
    return {**legacy, "no_contract": []}


def _write_inputs(tmp_path: Path) -> list[str]:
    parity = json.loads(PARITY.read_text())
    for name in ("candidates", "pr_state", "text_state"):
        (tmp_path / f"{name}.json").write_text(json.dumps(parity[name]))
    return [
        "bundle",
        "--candidates",
        str(tmp_path / "candidates.json"),
        "--pr-state",
        str(tmp_path / "pr_state.json"),
        "--text-state",
        str(tmp_path / "text_state.json"),
        "--max",
        "3",
        "--contract-state",
        str(_contract_state(tmp_path, parity["candidates"])),
        "--examined-memory",
        str(tmp_path / "examined.json"),
    ]


def _contract_state(tmp_path: Path, candidates: list[dict[str, Any]]) -> Path:
    path = tmp_path / "contract_state.json"
    path.write_text(
        json.dumps([{"id": c["id"], "has_contract": True} for c in candidates])
    )
    return path


MALFORMED_BUNDLES: list[tuple[str, dict[str, Any]]] = [
    ("held", {"held": "OMN-23"}),
    ("held.0", {"held": [7]}),
    ("held_count", {"held_count": -1}),
    ("totals.selected", {"totals": {**_valid_bundle()["totals"], "selected": "12"}}),
    ("totals.held", {"totals": {**_valid_bundle()["totals"], "held": True}}),
    ("waiting.0.reason", {"waiting": [{"id": "OMN-1", "reason": 3}]}),
    ("candidates.0.sneaky", {"candidates": [{"id": "OMN-71", "sneaky": 1}]}),
    ("pr_state.0.merged", {"pr_state": [{"id": "OMN-71", "merged": "x", "open": []}]}),
]


@pytest.mark.parametrize(("field", "change"), MALFORMED_BUNDLES)
def test_scheduled_job_contract_scope_bundle_names_the_malformed_field(
    field: str, change: dict[str, Any]
) -> None:
    bad = {**_valid_bundle(), **change}
    with pytest.raises(ScopeContractError) as caught:
        parse_scope_bundle(json.dumps(bad), file="bundle.json")
    assert field in caught.value.fields
    assert "bundle.json" in str(caught.value)


def test_scheduled_job_contract_scope_bundle_missing_field_is_named() -> None:
    bad = _valid_bundle()
    del bad["waiting_by_reason"]
    with pytest.raises(ScopeContractError) as caught:
        parse_scope_bundle(json.dumps(bad), file="bundle.json")
    assert caught.value.fields == ("waiting_by_reason",)


@pytest.mark.parametrize("text", ["", "   ", "not json", "[]", "BUNDLE_FAILED: exit 2"])
def test_scheduled_job_contract_scope_bundle_never_reads_garbage_as_empty(
    text: str,
) -> None:
    with pytest.raises(ScopeContractError, match=r"bundle\.json"):
        parse_scope_bundle(text, file="bundle.json")


def test_scheduled_job_contract_scope_bundle_positive_control() -> None:
    bundle = parse_scope_bundle(json.dumps(_valid_bundle()), file="bundle.json")
    assert isinstance(bundle, ModelScopeBundle)
    assert bundle.held[0] == "OMN-23"
    assert bundle.held_count == 8


def test_scheduled_job_contract_selected_total_must_match_the_rows() -> None:
    bad = {**_valid_bundle(), "totals": {**_valid_bundle()["totals"], "selected": 99}}
    with pytest.raises(ScopeContractError, match=r"totals\.selected"):
        parse_scope_bundle(json.dumps(bad), file="bundle.json")


def test_scheduled_job_contract_input_rows_are_models_not_coerced_dicts() -> None:
    with pytest.raises(ScopeContractError) as caught:
        parse_scope_rows(
            json.dumps([{"id": "OMN-1"}, 5]),
            ModelScopeCandidateRow,
            file="--candidates c.json",
        )
    assert "[1]" in caught.value.fields[0]
    with pytest.raises(ScopeContractError, match="open"):
        parse_scope_rows(
            json.dumps([{"id": "OMN-1", "merged": []}]),
            ModelScopePrStateRow,
            file="--pr-state p.json",
        )


# The rows the closeout text-state command prints: every row carries ``error`` (empty when the
# ticket was read, the reason when its read failed), spelled here as that command writes them.
TEXT_STATE_ROW_READ: dict[str, Any] = {
    "id": "OMN-1",
    "text_sha": "0123456789abcdef",
    "last_closeout_comment_at": "2026-11-20T10:00:00Z",
    "held_text_sha": "",
    "waiting_on": [],
    "error": "",
}
TEXT_STATE_ROW_FAILED: dict[str, Any] = {
    "id": "OMN-2",
    "text_sha": "",
    "last_closeout_comment_at": "",
    "held_text_sha": "",
    "waiting_on": [],
    "error": "URLError: <urlopen error timed out>",
}


def test_text_state_rows_with_their_error_field_are_read_as_typed_rows() -> None:
    rows = parse_scope_rows(
        json.dumps([TEXT_STATE_ROW_READ, TEXT_STATE_ROW_FAILED]),
        ModelScopeTextStateRow,
        file="--text-state t.json",
    )
    assert [r.error for r in rows] == ["", "URLError: <urlopen error timed out>"]


def test_text_state_row_still_refuses_a_field_the_model_does_not_declare() -> None:
    with pytest.raises(ScopeContractError, match="sneaky"):
        parse_scope_rows(
            json.dumps([{**TEXT_STATE_ROW_READ, "sneaky": 1}]),
            ModelScopeTextStateRow,
            file="--text-state t.json",
        )


def test_bundle_reads_a_text_state_file_whose_rows_carry_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _write_inputs(tmp_path)
    parity = json.loads(PARITY.read_text())
    first, second = (c["id"] for c in parity["candidates"][:2])
    rows = [
        {**TEXT_STATE_ROW_READ, "id": first},
        {**TEXT_STATE_ROW_FAILED, "id": second},
    ]
    (tmp_path / "text_state.json").write_text(json.dumps(rows))
    assert runtime.main(argv) == 0
    printed = capsys.readouterr().out
    assert parse_scope_bundle(printed, file="b").schema_version == "scope_bundle.v1"


def test_scheduled_job_contract_valid_inputs_produce_a_bundle_the_reader_accepts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert runtime.main(_write_inputs(tmp_path)) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["schema_version"] == "scope_bundle.v1"
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(printed)
    assert runtime.main(["read-bundle", "--bundle", str(bundle_file)]) == 0
    assert isinstance(parse_scope_bundle(printed, file="b"), ModelScopeBundle)


def test_bundle_held_not_array_is_refused_by_the_reader(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = FIXTURES / "scope_bundle_held_not_array.json"
    assert json.loads(fixture.read_text())["held"] == "OMN-23"
    assert runtime.main(["read-bundle", "--bundle", str(fixture)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "closeout-scope: REFUSED" in captured.err
    assert "held" in captured.err
    assert str(fixture) in captured.err


def test_bundle_held_not_array_reader_exits_nonzero_as_the_workflow_calls_it() -> None:
    fixture = FIXTURES / "scope_bundle_held_not_array.json"
    done = subprocess.run(
        [sys.executable, "-m", RUNTIME_MODULE, "read-bundle", "--bundle", str(fixture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2
    assert done.stdout == ""
    assert "held" in done.stderr


def test_bundle_held_not_array_is_never_written_by_the_producer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = closer_scope_rules.scope_bundle

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {**real(*args, **kwargs), "held": "OMN-23"}

    argv = _write_inputs(tmp_path)
    monkeypatch.setattr(closer_scope_rules, "scope_bundle", broken)
    assert runtime.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "held" in captured.err
    assert not (tmp_path / "examined.json").exists()
    monkeypatch.setattr(closer_scope_rules, "scope_bundle", real)
    assert runtime.main(argv) == 0
    assert (tmp_path / "examined.json").exists()


# A row as the closeout text-state command (the producer, which another repository owns) prints it: every row
# carries ``error``, empty when the ticket was read, the failure's text when it was not. The shape
# is spelled here so the check runs on every runner without that repository.
def _producer_text_state_rows(
    candidates: list[dict[str, Any]], errors: dict[str, str]
) -> list[dict[str, Any]]:
    return [
        {
            "id": c["id"],
            "text_sha": "" if c["id"] in errors else "0123456789abcdef",
            "last_closeout_comment_at": "",
            "held_text_sha": "",
            "waiting_on": [],
            "error": errors.get(c["id"], ""),
        }
        for c in candidates
    ]


def _write_producer_text_state(tmp_path: Path, errors: dict[str, str]) -> list[str]:
    argv = _write_inputs(tmp_path)
    candidates = json.loads(PARITY.read_text())["candidates"]
    (tmp_path / "text_state.json").write_text(
        json.dumps(_producer_text_state_rows(candidates, errors))
    )
    return argv


def test_scheduled_job_contract_text_state_rows_as_the_producer_writes_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The live failure: the producer's ``error`` cell on each row was refused by the model."""
    candidates = json.loads(PARITY.read_text())["candidates"]
    rows = _producer_text_state_rows(candidates, {})
    parsed = parse_scope_rows(
        json.dumps(rows), ModelScopeTextStateRow, file="--text-state t.json"
    )
    assert [r.error for r in parsed] == [""] * len(rows)
    assert runtime.main(_write_producer_text_state(tmp_path, {})) == 0
    printed = capsys.readouterr().out
    bundle = parse_scope_bundle(printed, file="bundle")
    assert bundle.text_state
    assert all(r.error == "" for r in bundle.text_state)


def test_scheduled_job_contract_text_state_error_is_a_finding_never_a_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreadable ticket is named in the bundle's waiting rows, not dropped or read as clean."""
    unread = (
        "OMN-1"  # the first id in sort order: inside the bundle's capped waiting list
    )
    why = "URLError: <urlopen error timed out>"
    argv = _write_producer_text_state(tmp_path, {unread: why})
    assert runtime.main(argv) == 0
    bundle = parse_scope_bundle(capsys.readouterr().out, file="bundle")
    assert unread not in [c.id for c in bundle.candidates]
    reasons = {w.id: w.reason for w in bundle.waiting}
    assert reasons[unread] == f"text state unread: {why}"
    assert bundle.waiting_by_reason["text state unread"] == 1
    assert bundle.totals.waiting == bundle.waiting_count
    # the same inputs with the error cleared: the finding is the row's own, not a side effect
    assert runtime.main(_write_producer_text_state(tmp_path, {})) == 0
    again = parse_scope_bundle(capsys.readouterr().out, file="bundle")
    assert "text state unread" not in again.waiting_by_reason
    assert again.waiting_count == bundle.waiting_count - 1


def test_scheduled_job_contract_text_state_error_must_be_text() -> None:
    row = {"id": "OMN-1", "error": ["boom"]}
    with pytest.raises(ScopeContractError) as caught:
        parse_scope_rows(json.dumps([row]), ModelScopeTextStateRow, file="t.json")
    assert caught.value.fields == ("[0].error",)
    with pytest.raises(ScopeContractError) as caught:
        parse_scope_rows(
            json.dumps([{"id": "OMN-1", "sneaky": 1}]),
            ModelScopeTextStateRow,
            file="t.json",
        )
    assert caught.value.fields == ("[0].sneaky",)


def test_scheduled_job_contract_integration_record_names_the_field() -> None:
    good = (
        "artifact_type: ModelIntegrationRecord\nartifact_version: 1.0.0\n"
        "date: '2026-11-20'\nscope: explicit\nstatus: recorded\ntickets: []\n"
        "runtime_sha_match: []\nsurfaces: []\n"
    )
    assert read_integration_record(good, file="r.yaml").status == "recorded"
    bad = good.replace("tickets: []", "tickets: OMN-1")
    with pytest.raises(IntegrationRecordError) as caught:
        read_integration_record(bad, file="r.yaml")
    assert caught.value.fields == ("tickets",)
    with pytest.raises(IntegrationRecordError, match=r"r\.yaml"):
        read_integration_record("- a list\n", file="r.yaml")


def test_scheduled_job_contract_held_drafts_name_the_field() -> None:
    entry = {
        "ticket": "OMN-1",
        "comment_id": "c1",
        "comment_url": "",
        "author_role": "human",
        "stage": "verify",
        "reason_class": "unsourced",
        "reason": "no source",
        "asks": ["why"],
    }
    assert read_held_drafts([entry], file="sweep.json")[0].ticket == "OMN-1"
    with pytest.raises(HeldDraftListError, match="held is not an array"):
        read_held_drafts("OMN-1", file="sweep.json")
    with pytest.raises(HeldDraftListError) as caught:
        read_held_drafts([{**entry, "reason_class": "made_up"}], file="sweep.json")
    assert caught.value.fields == ("held[0].reason_class",)


@pytest.mark.parametrize("field", ["held", "needing", "accepted", "collected"])
def test_scheduled_job_contract_hourly_sweep_counts_are_exact_counts(
    field: str,
) -> None:
    assert ModelSweepResult.model_validate({field: 3, "failed": False}).failed is False
    for bad in (["OMN-1"], "3", -1, True):
        with pytest.raises(ValueError, match=field):
            ModelSweepResult.model_validate({field: bad, "failed": False})
    with pytest.raises(ValueError, match="held_by_class"):
        ModelSweepResult.model_validate({"held_by_class": {"timeout": "1"}})


def test_scheduled_job_contract_approved_work_file_names_the_field() -> None:
    assert (
        parse_approved_work_file('{"version": 1, "rows": []}', file="a.json").rows == []
    )
    for text, field in (
        ('{"version": 2, "rows": []}', "version"),
        ('{"version": 1, "rows": {}}', "rows"),
        ('{"version": 1}', "rows"),
        ('{"version": 1, "rows": [], "x": 1}', "x"),
    ):
        with pytest.raises(ApprovedWorkFileError) as caught:
            parse_approved_work_file(text, file="a.json")
        assert caught.value.fields == (field,)
        assert "a.json" in str(caught.value)
    with pytest.raises(ApprovedWorkFileError, match="not JSON"):
        parse_approved_work_file("{", file="a.json")


# The hand-written field checks the models replaced: a reader that tests a bundle's or a list
# file's own fields with isinstance/get is hand-parsing it.
HAND_CHECKS = (
    re.compile(r"isinstance\(\s*(rows|value|bundle|b)\s*,\s*(list|dict)\s*\)"),
    re.compile(r"value\.get\(\s*[\"']version[\"']\s*\)"),
    re.compile(r"isinstance\(\s*value\.get\("),
    re.compile(r"bundle\[[\"'](held|waiting|candidates|totals)[\"']\]\s*,\s*list"),
)
NODES = Path(runtime.__file__).parents[1]
CONVERTED_READERS = (
    NODES / "node_dod_closeout_sweep_compute" / "runtime_dod_closeout_sweep.py",
    NODES / "node_lab_fill_effect" / "protocols" / "local_lab_fill_enumerate_facts.py",
)


def _hand_checks(text: str) -> list[str]:
    return [m.group(0) for pattern in HAND_CHECKS for m in pattern.finditer(text)]


def _approved_work_loader_text() -> str:
    adapters = (
        NODES / "node_lab_fill_effect" / "protocols" / "local_lab_fill_adapters.py"
    ).read_text()
    start = adapters.index("class LocalApprovedWork")
    return adapters[start : adapters.index("def depth", start)]


def test_no_hand_parsed_bundle_in_the_converted_readers() -> None:
    for reader in CONVERTED_READERS:
        text = reader.read_text()
        if reader.name == "local_lab_fill_enumerate_facts.py":
            start = text.index("class LocalApprovedRows")
            text = text[start : text.index("def fresh_watcher_state", start)]
        assert _hand_checks(text) == [], reader.name
    assert _hand_checks(_approved_work_loader_text()) == []


def test_no_hand_parsed_bundle_positive_control() -> None:
    assert _hand_checks("if not isinstance(rows, list):\n    raise X") != []
    assert _hand_checks('or value.get("version") != 1') != []
    assert _hand_checks("parse_approved_work_file(text, file=f)") == []


def test_scheduled_job_contract_beside_registry_clone_follows_a_symlinked_root(
    tmp_path: Path,
) -> None:
    """The live warning ``clones not read``: a registry root that is a symlink has its sibling
    clones beside the link's target, which is where the shell's ``..`` reads them."""
    from omnimarket.models.closeout_scope_overlay import (
        EnumCloneLocation,
        ModelPrRepository,
    )
    from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_pr_sources import (
        clone_path,
    )

    real = tmp_path / "data" / "registry"
    real.mkdir(parents=True)
    (tmp_path / "data" / "sibling").mkdir()
    link = tmp_path / "home" / "registry"
    link.parent.mkdir()
    link.symlink_to(real)
    repo = ModelPrRepository(
        name="sibling",
        default_branch="main",
        location=EnumCloneLocation.BESIDE_REGISTRY,
    )
    assert clone_path(link, repo) == (tmp_path / "data" / "sibling").resolve()
    assert clone_path(link, repo).is_dir()


def test_bundle_held_not_array_and_error_rows_reader_reads_the_one_and_refuses_the_other(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bundle whose text-state rows carry ``error`` (the producer's own cell) is read; the
    same bundle with ``held`` a string is refused naming ``held``, never read as empty."""
    assert runtime.main(_write_producer_text_state(tmp_path, {"OMN-1": "boom"})) == 0
    printed = capsys.readouterr().out
    carried = json.loads(printed)
    assert carried["text_state"], (
        "the bundle must carry text-state rows for this control"
    )
    carried["text_state"][0]["error"] = "boom"
    good = tmp_path / "bundle.json"
    good.write_text(json.dumps(carried))
    assert runtime.main(["read-bundle", "--bundle", str(good)]) == 0
    assert '"error":"boom"' in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**carried, "held": "OMN-23"}))
    assert runtime.main(["read-bundle", "--bundle", str(bad)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "held" in captured.err
