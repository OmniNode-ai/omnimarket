# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20071 — the DurableEvidenceGate reads the product repository first.

``contract_on_occ_main`` used to read the ticket's contract from the
onex_change_control governance ref only, so a ticket whose contract lives in
the repository its PR merged into (``contracts/<TICKET>.yaml`` in omnimarket or
omnibase_infra, both cut over) could not pass this gate without an OCC copy.

The resolution order is the omniclaude Done gate's: the contract is read from
each merged PR's repository at the merge commit; of several merged PRs in one
repository that carry it, the newest merged one decides; the verdict is the
``repo-evidence / dod-verify`` check run on that PR's head, whose contract must
equal the merged one; every labelled criterion of the ticket must be bound.
Only a ticket none of whose merged PRs carries an adopted repo contract falls
back to OCC, and there it decides exactly as before (replayed below from
inputs whose decisions were recorded at 3fe55c842, before this change).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDurableEvidenceCheck,
    EnumDurableEvidenceStatus,
    EnumRepoContractReadStatus,
    EnumRepoEvidenceOutcome,
    ModelDurableEvidenceCheckResult,
    ModelDurableEvidenceGateResult,
    ModelRepoContractRead,
    ModelRepoEvidenceCheckRun,
    ModelTicketMergedPr,
)
from omnimarket.nodes.node_dod_verify.services import durable_evidence_gate
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    REPO_EVIDENCE_APP_SLUG,
    REPO_EVIDENCE_CHECK_NAME,
    DurableEvidenceGate,
    evaluate_repo_evidence,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "omn20071_occ_only_gate_replay.json"
_REPO = "OmniNode-ai/omnimarket"
_TICKET = "OMN-90100"
_HEAD = "a" * 40
_MERGE = "b" * 40
_OLD_HEAD = "c" * 40
_OLD_MERGE = "d" * 40
_DESCRIPTION = (
    "Implemented in omnimarket.\n\n"
    "## Acceptance criteria\n\n"
    "- [ ] AC1: the first behaviour -- falsifier: uv run pytest tests/a.py\n"
    "- [ ] AC2: the second behaviour -- falsifier: uv run pytest tests/b.py\n"
)


# --------------------------------------------------------------------------- #
# Recorded OCC-only inputs -> deterministic probes (the base-compatible part).
# --------------------------------------------------------------------------- #


def _occ_gate(case: dict[str, Any], **repo_readers: Any) -> DurableEvidenceGate:
    occ = case["occ"]
    assert isinstance(occ, dict)
    pr_view = {
        (p["repo"], p["number"]): (p["state"], p["merge_commit_oid"])
        for p in case["pr_view"]
    }
    pr_commits = {
        (p["repo"], p["number"]): tuple(p["oids"]) for p in case["pr_commits"]
    }
    tags = {(t["repo"], t["sha"]): t["tags"] for t in case["release_tags"]}
    index = {(i["distribution"], i["version"]): i["files"] for i in case["index_files"]}

    def release_tags_containing(repo: str, commit_sha: str) -> tuple[str, ...] | None:
        if (repo, commit_sha) in tags:
            found = tags[(repo, commit_sha)]
            return None if found is None else tuple(found)
        return ("v9.9.9",)

    def index_release_files(distribution: str, version: str) -> frozenset[str] | None:
        if (distribution, version) in index:
            files = index[(distribution, version)]
            return None if files is None else frozenset(files)
        return frozenset({"bdist_wheel", "sdist"})

    def gh_pr_view(repo: str, pr_number: int) -> tuple[str, str | None]:
        return pr_view[(repo, pr_number)]

    return DurableEvidenceGate(
        is_receipt_tracked=lambda _p, _r, _d: bool(occ["tracked"]),
        gh_pr_view=gh_pr_view,
        pr_commits=lambda repo, n: pr_commits.get((repo, n), ()),
        load_contract_on_ref=lambda _p, _r, _rel: occ["contract_on_ref"],
        load_receipts_on_ref=lambda _p, _r, _d: [dict(r) for r in occ["receipts"]],
        release_tags_containing=release_tags_containing,
        index_release_files=index_release_files,
        occ_repo_path="/fake/onex_change_control",
        **repo_readers,
    )


def _cases() -> list[dict[str, Any]]:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert isinstance(cases, list)
    return cases


# --------------------------------------------------------------------------- #
# Product-repository readers.
# --------------------------------------------------------------------------- #


def _contract(*binds: list[str], ticket_id: str = _TICKET) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "dod_evidence": [
            {
                "id": f"dod-{i}",
                "description": "bound test",
                "checks": [
                    {
                        "check_type": "test_passes",
                        "check_value": f"uv run pytest t{i}.py",
                    }
                ],
                "binds_ac": labels,
            }
            for i, labels in enumerate(binds, start=1)
        ],
    }


def _found(contract: dict[str, object]) -> ModelRepoContractRead:
    return ModelRepoContractRead(
        status=EnumRepoContractReadStatus.FOUND, contract=contract
    )


_ABSENT = ModelRepoContractRead(status=EnumRepoContractReadStatus.ABSENT)


def _run(
    run_id: int,
    conclusion: str | None = "success",
    *,
    status: str = "completed",
    name: str = REPO_EVIDENCE_CHECK_NAME,
    app_slug: str = REPO_EVIDENCE_APP_SLUG,
) -> ModelRepoEvidenceCheckRun:
    return ModelRepoEvidenceCheckRun(
        id=run_id, name=name, app_slug=app_slug, status=status, conclusion=conclusion
    )


def _pr(
    number: int = 3600,
    *,
    head: str = _HEAD,
    merge: str = _MERGE,
    merged_at: str = "2026-10-09T12:00:00Z",
    repo: str = _REPO,
) -> ModelTicketMergedPr:
    return ModelTicketMergedPr(
        repo=repo,
        pr_number=number,
        head_sha=head,
        merge_commit_sha=merge,
        merged_at=merged_at,
    )


def _readers(
    contracts: dict[tuple[str, str], ModelRepoContractRead],
    runs: dict[tuple[str, str], tuple[ModelRepoEvidenceCheckRun, ...] | None],
) -> dict[str, Any]:
    def read_repo_contract(
        repo: str, ref: str, ticket_id: str
    ) -> ModelRepoContractRead:
        return contracts.get((repo, ref), _ABSENT)

    def read_repo_check_runs(
        repo: str, sha: str
    ) -> tuple[ModelRepoEvidenceCheckRun, ...] | None:
        return runs.get((repo, sha), ())

    return {
        "read_repo_contract": read_repo_contract,
        "read_repo_check_runs": read_repo_check_runs,
    }


def _no_occ_gate(**repo_readers: Any) -> DurableEvidenceGate:
    """A gate whose OCC side holds nothing for the ticket: no receipt, no contract."""

    def refuse_pr_probe(repo: str, pr_number: int) -> tuple[str, str | None]:
        msg = f"OCC receipt PR probe must not run: {repo}#{pr_number}"
        raise AssertionError(msg)

    return DurableEvidenceGate(
        is_receipt_tracked=lambda _p, _r, _d: False,
        gh_pr_view=refuse_pr_probe,
        pr_commits=lambda _repo, _n: (),
        load_contract_on_ref=lambda _p, _r, _rel: None,
        load_receipts_on_ref=lambda _p, _r, _d: [],
        release_tags_containing=lambda _repo, _sha: ("v0.4.309",),
        index_release_files=lambda _d, _v: frozenset({"bdist_wheel", "sdist"}),
        occ_repo_path="/fake/onex_change_control",
        **repo_readers,
    )


def _evaluate(
    gate: DurableEvidenceGate,
    merged_prs: tuple[ModelTicketMergedPr, ...],
    *,
    contract: dict[str, object] | None = None,
    description: str = _DESCRIPTION,
    labels: frozenset[str] = frozenset({"source-done"}),
) -> ModelDurableEvidenceGateResult:
    return gate.evaluate_default(
        ticket_id=_TICKET,
        contract=contract if contract is not None else _contract(["AC1"], ["AC2"]),
        ticket_labels=labels,
        merged_prs=merged_prs,
        ticket_description=description,
    )


def _check(
    result: ModelDurableEvidenceGateResult, check: EnumDurableEvidenceCheck
) -> ModelDurableEvidenceCheckResult:
    return next(c for c in result.checks if c.check == check)


def _green_readers(contract: dict[str, object]) -> dict[str, Any]:
    return _readers(
        {(_REPO, _MERGE): _found(contract), (_REPO, _HEAD): _found(contract)},
        {(_REPO, _HEAD): (_run(7001),)},
    )


# --------------------------------------------------------------------------- #
# AC4: a cut-over repository's contract is admitted on its own evidence.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
class TestRepoFirstAdmits:
    def test_admits_bound_repo_contract_with_green_run_and_no_occ_contract(
        self,
    ) -> None:
        contract = _contract(["AC1"], ["AC2"])
        result = _evaluate(_no_occ_gate(**_green_readers(contract)), (_pr(),))

        assert result.status == EnumDurableEvidenceStatus.PASS, result.checks
        governing = _check(result, EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN)
        assert governing.passed is True
        assert f"{_REPO}#3600" in governing.message
        assert "run 7001" in governing.message
        for check in (
            EnumDurableEvidenceCheck.RECEIPT_TRACKED,
            EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT,
        ):
            assert "Not applicable" in _check(result, check).message

    def test_admits_when_newest_merged_pr_is_green_over_a_superseded_red_run(
        self,
    ) -> None:
        contract = _contract(["AC1"], ["AC2"])
        old = _pr(
            3500, head=_OLD_HEAD, merge=_OLD_MERGE, merged_at="2026-10-01T00:00:00Z"
        )
        readers = _readers(
            {
                (_REPO, _MERGE): _found(contract),
                (_REPO, _HEAD): _found(contract),
                (_REPO, _OLD_MERGE): _found(_contract(["AC1"])),
                (_REPO, _OLD_HEAD): _found(_contract(["AC1"])),
            },
            {
                (_REPO, _HEAD): (_run(7001),),
                (_REPO, _OLD_HEAD): (_run(5001, "failure"),),
            },
        )

        result = _evaluate(_no_occ_gate(**readers), (old, _pr()))

        assert result.status == EnumDurableEvidenceStatus.PASS, result.checks

    def test_admits_over_an_occ_copy_carrying_items_the_repo_contract_lacks(
        self,
    ) -> None:
        merged = _contract(["AC1"], ["AC2"])
        occ_copy = _contract(["AC1"], ["AC2"], ["AC2"])
        result = _evaluate(
            _no_occ_gate(**_green_readers(merged)), (_pr(),), contract=occ_copy
        )

        assert result.status == EnumDurableEvidenceStatus.PASS, result.checks

    def test_admits_defect_ticket_whose_repo_contract_links_a_prevention_gate(
        self,
    ) -> None:
        merged = {**_contract(["AC1"], ["AC2"]), "prevention_gate": "ci.yml"}
        result = _evaluate(
            _no_occ_gate(**_green_readers(merged)),
            (_pr(),),
            contract=_contract(["AC1"], ["AC2"]),
            labels=frozenset({"source-done", "bug"}),
        )

        assert result.status == EnumDurableEvidenceStatus.PASS, result.checks

    def test_admits_verdict_names_the_binding_items(self) -> None:
        contract = _contract(["AC1"], ["AC2"])
        verdict = evaluate_repo_evidence(
            _TICKET,
            _DESCRIPTION,
            (_pr(),),
            **_green_readers(contract),
        )

        assert verdict.outcome is EnumRepoEvidenceOutcome.PASSED
        assert "AC1<-dod-1" in verdict.detail
        assert "AC2<-dod-2" in verdict.detail


# --------------------------------------------------------------------------- #
# AC4: the repo path keeps the gate's strictness.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
class TestRepoDiagnosticEscaping:
    @pytest.mark.parametrize(
        "field",
        ["source", "error", "ticket_id", "named_ticket", "status", "criterion", "item"],
    )
    def test_untrusted_values_are_inert_in_verdict_details(self, field: str) -> None:
        payload = '<script>"&\r\n\x1b[31m\x00\u2028'
        contract = _contract(["AC1"], ["AC2"])
        pr = _pr(repo=payload) if field == "source" else _pr()
        ticket = payload if field == "ticket_id" else _TICKET
        contract["ticket_id"] = payload if field == "named_ticket" else ticket
        read = _found(contract)
        if field in {"source", "error", "ticket_id"}:
            read = ModelRepoContractRead(
                status=EnumRepoContractReadStatus.ERROR,
                error=payload if field == "error" else "HTTP 502",
            )
        description = _DESCRIPTION
        if field == "criterion":
            criterion = payload.replace("\r\n", "")
            description += f"- [ ] an unlabelled criterion {criterion}\n"
        if field == "item":
            contract["dod_evidence"] = [{"id": payload, "binds_ac": ["AC1", "AC2"]}]
            read = _found(contract)
        readers = _readers(
            {(pr.repo, _MERGE): read, (pr.repo, _HEAD): read},
            {
                (pr.repo, _HEAD): (
                    _run(7001, status=payload if field == "status" else "completed"),
                )
            },
        )

        verdict = evaluate_repo_evidence(ticket, description, (pr,), **readers)

        expected = (
            EnumRepoEvidenceOutcome.PASSED
            if field == "item"
            else EnumRepoEvidenceOutcome.REFUSED
        )
        assert verdict.outcome is expected
        assert "&lt;script&gt;" in verdict.detail
        assert "<script>" not in verdict.detail
        assert not any(char in verdict.detail for char in "\r\n\x1b\x00\u2028")
        assert "\\x1b" in verdict.detail or "\\u001b" in verdict.detail
        if field == "item":
            # Formatting a diagnostic must not rewrite the governing evidence.
            assert verdict.governing_contracts == (contract,)


@pytest.mark.unit
class TestRepoFirstRefuses:
    def _refused(self, result: ModelDurableEvidenceGateResult, needle: str) -> None:
        assert result.status == EnumDurableEvidenceStatus.FAIL
        governing = _check(result, EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN)
        assert governing.passed is False
        assert needle in governing.message, governing.message

    def test_refuses_repo_contract_leaving_one_criterion_unbound(self) -> None:
        contract = _contract(["AC1"])
        result = _evaluate(
            _no_occ_gate(**_green_readers(contract)), (_pr(),), contract=contract
        )
        self._refused(result, "AC2")

    def test_refuses_red_run_on_the_merged_head(self) -> None:
        contract = _contract(["AC1"], ["AC2"])
        readers = _readers(
            {(_REPO, _MERGE): _found(contract), (_REPO, _HEAD): _found(contract)},
            {(_REPO, _HEAD): (_run(7001, "failure"),)},
        )
        self._refused(
            _evaluate(_no_occ_gate(**readers), (_pr(),)), "conclusion=failure"
        )

    def test_refuses_rerun_in_progress_over_an_older_success(self) -> None:
        contract = _contract(["AC1"], ["AC2"])
        readers = _readers(
            {(_REPO, _MERGE): _found(contract), (_REPO, _HEAD): _found(contract)},
            {
                (_REPO, _HEAD): (
                    _run(7001),
                    _run(7002, None, status="in_progress"),
                )
            },
        )
        self._refused(_evaluate(_no_occ_gate(**readers), (_pr(),)), "run 7002")

    def test_refuses_contract_changed_between_verified_head_and_merge(self) -> None:
        merged = _contract(["AC1"], ["AC2"])
        readers = _readers(
            {
                (_REPO, _MERGE): _found(merged),
                (_REPO, _HEAD): _found(_contract(["AC1"])),
            },
            {(_REPO, _HEAD): (_run(7001),)},
        )
        self._refused(_evaluate(_no_occ_gate(**readers), (_pr(),)), "contract changed")

    def test_refuses_unreadable_repo_contract(self) -> None:
        readers = _readers(
            {
                (_REPO, _MERGE): ModelRepoContractRead(
                    status=EnumRepoContractReadStatus.ERROR, error="HTTP 502"
                )
            },
            {},
        )
        self._refused(_evaluate(_no_occ_gate(**readers), (_pr(),)), "HTTP 502")

    def test_refuses_unreadable_check_runs(self) -> None:
        contract = _contract(["AC1"], ["AC2"])
        readers = _readers(
            {(_REPO, _MERGE): _found(contract), (_REPO, _HEAD): _found(contract)},
            {(_REPO, _HEAD): None},
        )
        self._refused(_evaluate(_no_occ_gate(**readers), (_pr(),)), "unreadable")

    def test_refuses_contract_naming_another_ticket(self) -> None:
        contract = _contract(["AC1"], ["AC2"], ticket_id="OMN-1")
        self._refused(
            _evaluate(_no_occ_gate(**_green_readers(contract)), (_pr(),)), "OMN-1"
        )

    def test_refuses_unlabelled_criterion(self) -> None:
        contract = _contract(["AC1"], ["AC2"])
        description = _DESCRIPTION + "- [ ] a criterion with no label\n"
        self._refused(
            _evaluate(
                _no_occ_gate(**_green_readers(contract)),
                (_pr(),),
                description=description,
            ),
            "no label",
        )

    def test_refuses_ticket_with_no_readable_criterion(self) -> None:
        contract = _contract(["AC1"], ["AC2"])
        self._refused(
            _evaluate(
                _no_occ_gate(**_green_readers(contract)), (_pr(),), description=""
            ),
            "no acceptance criterion",
        )

    def test_refuses_missing_repo_contract_without_an_occ_contract(self) -> None:
        readers = _readers({}, {})
        self._refused(_evaluate(_no_occ_gate(**readers), (_pr(),)), "is not present on")

    def test_refuses_newest_pr_without_a_run_falls_back_to_occ(self) -> None:
        contract = _contract(["AC1"], ["AC2"])
        readers = _readers(
            {(_REPO, _MERGE): _found(contract), (_REPO, _HEAD): _found(contract)},
            {(_REPO, _HEAD): (_run(7001, name="some other check"),)},
        )
        self._refused(_evaluate(_no_occ_gate(**readers), (_pr(),)), "is not present on")

    def test_refuses_defect_ticket_whose_repo_contract_links_no_prevention(
        self,
    ) -> None:
        merged = _contract(["AC1"], ["AC2"])
        local = {**merged, "prevention_gate": ".github/workflows/ci.yml"}
        result = _evaluate(
            _no_occ_gate(**_green_readers(merged)),
            (_pr(),),
            contract=local,
            labels=frozenset({"source-done", "bug"}),
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        assert _check(result, EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN).passed
        assert not _check(
            result, EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE
        ).passed

    def test_refuses_green_repo_ticket_without_a_done_class_label(self) -> None:
        contract = _contract(["AC1"], ["AC2"])
        result = _evaluate(
            _no_occ_gate(**_green_readers(contract)), (_pr(),), labels=frozenset()
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        assert _check(result, EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN).passed
        assert not _check(result, EnumDurableEvidenceCheck.DONE_CLASS_LABEL).passed

    def test_refuses_green_repo_ticket_merged_but_unreleased(self) -> None:
        contract = _contract(["AC1"], ["AC2"])
        gate = DurableEvidenceGate(
            is_receipt_tracked=lambda _p, _r, _d: False,
            gh_pr_view=lambda _repo, _n: ("MERGED", None),
            pr_commits=lambda _repo, _n: (),
            load_contract_on_ref=lambda _p, _r, _rel: None,
            load_receipts_on_ref=lambda _p, _r, _d: [],
            release_tags_containing=lambda _repo, _sha: (),
            index_release_files=lambda _d, _v: frozenset(),
            occ_repo_path="/fake/onex_change_control",
            **_green_readers(contract),
        )
        result = _evaluate(gate, (_pr(),))

        assert result.status == EnumDurableEvidenceStatus.FAIL
        assert not _check(
            result, EnumDurableEvidenceCheck.RELEASED_ON_PUBLISHING_REPO
        ).passed


# --------------------------------------------------------------------------- #
# AC5: an OCC-only ticket decides exactly as it did before the change.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
class TestOccOnlyReplay:
    def test_replay_fixture_was_recorded_before_the_change(self) -> None:
        data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        assert data["base_commit"] == "3fe55c842"
        statuses = {case["decision"]["status"] for case in data["cases"]}
        # Positive control: the recorded set holds both admits and refusals.
        assert statuses == {"pass", "fail"}

    @pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["name"]))
    def test_replay_occ_only_ticket_decides_as_recorded(
        self, case: dict[str, Any]
    ) -> None:
        merged_prs = tuple(
            ModelTicketMergedPr.model_validate(pr) for pr in case["merged_prs"]
        )
        # Every merged PR's repository is read and holds no contract, so the
        # repo path is consulted and does not engage.
        consulted: list[tuple[str, str]] = []

        def read_repo_contract(
            repo: str, ref: str, ticket_id: str
        ) -> ModelRepoContractRead:
            consulted.append((repo, ref))
            return _ABSENT

        def read_repo_check_runs(
            repo: str, sha: str
        ) -> tuple[ModelRepoEvidenceCheckRun, ...] | None:
            msg = "no repo contract, so no check run is read"
            raise AssertionError(msg)

        gate = _occ_gate(
            case,
            read_repo_contract=read_repo_contract,
            read_repo_check_runs=read_repo_check_runs,
        )
        result = gate.evaluate_default(
            ticket_id=str(case["ticket_id"]),
            contract=case["contract"],
            ticket_labels=frozenset(case["labels"]),
            merged_prs=merged_prs,
            ticket_description=_DESCRIPTION,
        )

        assert result.model_dump(mode="json") == case["decision"]
        assert consulted == [(pr.repo, pr.merge_commit_sha) for pr in merged_prs]


# --------------------------------------------------------------------------- #
# AC6: no new non-canonical file; the change lives in the node's own modules.
# --------------------------------------------------------------------------- #

_NODE_DIR = Path(durable_evidence_gate.__file__).resolve().parents[1]
_NODE_FILES = frozenset(
    {
        "__init__.py",
        "__main__.py",
        "contract.yaml",
        "metadata.yaml",
        "handlers/__init__.py",
        "handlers/dod_evidence_local_source.py",
        "handlers/handler_dod_evidence_github_effect.py",
        "handlers/handler_dod_verify.py",
        # OMN-20917: the S7 replay's handler, models and pure rules.
        "handlers/handler_occ_replay.py",
        "handlers/handler_runtime_sha_verify.py",
        "models/__init__.py",
        "models/model_ac_binding_retirement.py",
        "models/model_ac_falsifier_command.py",
        "models/model_dod_acceptance_summary.py",
        "models/model_dod_contract_subject.py",
        "models/model_dod_evidence_github_lookup.py",
        "models/model_dod_verify_completed_event.py",
        "models/model_dod_verify_start_command.py",
        "models/model_dod_verify_state.py",
        "models/model_durable_evidence_gate.py",
        "models/model_occ_replay.py",
        "models/model_occ_verdict_difference.py",
        "services/__init__.py",
        "services/ac_binding_retirements.py",
        "services/ac_falsifier_checks.py",
        "services/behavior_check_execution.py",
        "services/check_proof_class.py",
        "services/contract_subject.py",
        "services/durable_evidence_gate.py",
        "services/evidence_collector.py",
        "services/occ_replay.py",
        "services/occ_verdict_difference.py",
        "services/receipt_bound_evidence.py",
        "services/released_evidence.py",
        "services/runtime_ops_readback.py",
    }
)


@pytest.mark.unit
class TestCanonicalShape:
    def test_no_module_added_and_repo_first_lives_in_the_gate_modules(self) -> None:
        present = frozenset(
            str(p.relative_to(_NODE_DIR))
            for p in _NODE_DIR.rglob("*")
            if p.is_file() and p.suffix in {".py", ".yaml"}
        )
        assert present == _NODE_FILES

        gate_module = durable_evidence_gate.__name__
        model_module = ModelTicketMergedPr.__module__
        assert evaluate_repo_evidence.__module__ == gate_module
        assert model_module.endswith(".models.model_durable_evidence_gate")
        for model in (ModelRepoContractRead, ModelRepoEvidenceCheckRun):
            assert model.__module__ == model_module
