# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for DurableEvidenceGate.

The gate refuses Linear Done transitions unless the ticket's product-repository
contract and the check run on its merged head back it (OMN-20071). A ticket no
product repository governs is refused and decided nowhere else (OMN-20074).

The label and repair-to-ratchet checks keep their meaning on that path; their
cases run over an all-green product-repository contract. The receipt-citation
parsers the gate module still carries are covered at the end of the file.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDefectLabel,
    EnumDoneClassLabel,
    EnumDurableEvidenceCheck,
    EnumDurableEvidenceStatus,
    EnumRepoContractReadStatus,
    ModelDurableEvidenceGateResult,
    ModelRepoContractRead,
    ModelRepoEvidenceCheckRun,
    ModelTicketMergedPr,
)
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    REPO_EVIDENCE_APP_SLUG,
    REPO_EVIDENCE_CHECK_NAME,
    DurableEvidenceGate,
    DurableEvidenceGateError,
    default_contract_path,
    extract_receipt_merge_commits,
    parse_pr_url,
)

_TICKET = "OMN-9855"
_REPO = "OmniNode-ai/omnibase_core"
_HEAD = "a" * 40
_MERGE = "b" * 40
_DESCRIPTION = (
    "## Acceptance criteria\n\n"
    "- [ ] AC1: it works -- falsifier: uv run pytest tests/a.py\n"
)


def _ticket_contract(
    *,
    evidence_id: str = "dod-001",
) -> dict[str, object]:
    """Build a schema-valid contract dict with one evidence check bound to AC1."""
    return {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "dod_evidence": [
            {
                "id": evidence_id,
                "description": "Code change shipped",
                "checks": [
                    {"check_type": "test_passes", "check_value": "uv run pytest a.py"},
                ],
                "binds_ac": ["AC1"],
            }
        ],
    }


def _receipt(
    *,
    repo: str = "OmniNode-ai/omnibase_core",
    pr_number: int = 949,
    commit_sha: str = "abcdef1234567890abcdef1234567890abcdef12",
    evidence_id: str = "dod-001",
    check_type: str = "command",
    status: str = "PASS",
) -> dict[str, object]:
    """Build a schema-valid receipt payload with a PR/commit binding."""
    return {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "evidence_item_id": evidence_id,
        "check_type": check_type,
        "check_value": "true",
        "status": status,
        "run_timestamp": "2026-06-19T00:00:00Z",
        "commit_sha": commit_sha,
        "runner": "worker",
        "verifier": "reviewer",
        "probe_command": (
            f"gh pr view {pr_number} --repo {repo} --json number,url,state,mergeCommit"
        ),
        "probe_stdout": (
            f'{{"number":{pr_number},"url":"https://github.com/{repo}/pull/'
            f'{pr_number}","state":"MERGED","mergeCommit":{{"oid":"{commit_sha}"}}}}'
        ),
        "pr_number": pr_number,
    }


def _merged_pr() -> ModelTicketMergedPr:
    return ModelTicketMergedPr(
        repo=_REPO,
        pr_number=949,
        head_sha=_HEAD,
        merge_commit_sha=_MERGE,
        merged_at="2026-10-09T12:00:00Z",
    )


def _make_gate(
    *,
    contract_in_repo: dict[str, object] | None = None,
    released_tags: dict[tuple[str, str], tuple[str, ...] | None] | None = None,
    released_index: dict[tuple[str, str], frozenset[str] | None] | None = None,
) -> DurableEvidenceGate:
    """A gate over deterministic in-memory product-repository readers.

    ``contract_in_repo`` is the contract the merged PR carries; ``None`` means
    the repository carries none, so no product repository governs the ticket.
    """

    def read_repo_contract(
        repo: str, ref: str, ticket_id: str
    ) -> ModelRepoContractRead:
        if contract_in_repo is None:
            return ModelRepoContractRead(status=EnumRepoContractReadStatus.ABSENT)
        return ModelRepoContractRead(
            status=EnumRepoContractReadStatus.FOUND, contract=contract_in_repo
        )

    def read_repo_check_runs(
        repo: str, sha: str
    ) -> tuple[ModelRepoEvidenceCheckRun, ...] | None:
        return (
            ModelRepoEvidenceCheckRun(
                id=7001,
                name=REPO_EVIDENCE_CHECK_NAME,
                app_slug=REPO_EVIDENCE_APP_SLUG,
                status="completed",
                conclusion="success",
            ),
        )

    return DurableEvidenceGate(
        release_tags_containing=_released_tags_stub(released_tags),
        index_release_files=_released_index_stub(released_index),
        read_repo_contract=read_repo_contract,
        read_repo_check_runs=read_repo_check_runs,
    )


def _evaluate(
    gate: DurableEvidenceGate,
    *,
    contract: dict[str, object] | None = None,
    labels: frozenset[str] = frozenset(),
) -> ModelDurableEvidenceGateResult:
    return gate.evaluate(
        ticket_id=_TICKET,
        contract=contract if contract is not None else _ticket_contract(),
        ticket_labels=labels,
        merged_prs=(_merged_pr(),),
        ticket_description=_DESCRIPTION,
    )


def _check(
    result: ModelDurableEvidenceGateResult, check: EnumDurableEvidenceCheck
) -> Any:
    return next(c for c in result.checks if c.check == check)


# OMN-18010: the released-is-Done probes are REQUIRED gate constructor
# arguments — there is no unconfigured/skip path. These stubs answer "released"
# for every input, so the pre-OMN-18010 behaviour of the cases in this file is
# unchanged and the new check is exercised on its own terms in
# tests/unit/nodes/node_dod_verify/test_omn_18010_released_gate.py.
def _released_tags_stub(
    table: dict[tuple[str, str], tuple[str, ...] | None] | None = None,
):
    def probe(repo: str, commit_sha: str) -> tuple[str, ...] | None:
        if table is not None and (repo, commit_sha) in table:
            return table[(repo, commit_sha)]
        return ("v9.9.9",)

    return probe


def _released_index_stub(
    table: dict[tuple[str, str], frozenset[str] | None] | None = None,
):
    def probe(distribution: str, version: str) -> frozenset[str] | None:
        if table is not None and (distribution, version) in table:
            return table[(distribution, version)]
        return frozenset({"bdist_wheel", "sdist"})

    return probe


@pytest.mark.unit
class TestDurableEvidenceGate:
    """Behaviour of the durable-evidence gate across pass/fail cases."""

    def test_green_repo_contract_with_done_class_label_passes(self) -> None:
        gate = _make_gate(contract_in_repo=_ticket_contract())
        result = _evaluate(
            gate, labels=frozenset({EnumDoneClassLabel.SOURCE_DONE.value})
        )
        assert result.status == EnumDurableEvidenceStatus.PASS, result.checks
        assert _check(result, EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN).passed

    def test_ticket_no_product_repository_governs_is_refused(self) -> None:
        """No merged PR carries the contract: refused, and nothing else decides."""
        gate = _make_gate(contract_in_repo=None)
        result = _evaluate(
            gate, labels=frozenset({EnumDoneClassLabel.SOURCE_DONE.value})
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        governing = _check(result, EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN)
        assert governing.passed is False
        assert "no product repository governs" in governing.message
        assert "OCC PR" not in governing.message
        assert _check(result, EnumDurableEvidenceCheck.DONE_CLASS_LABEL).passed is False

    def test_ticket_with_no_merged_pr_is_refused(self) -> None:
        gate = _make_gate(contract_in_repo=_ticket_contract())
        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=_ticket_contract(),
            ticket_labels=frozenset({EnumDoneClassLabel.SOURCE_DONE.value}),
            merged_prs=(),
            ticket_description=_DESCRIPTION,
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        governing = _check(result, EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN)
        assert governing.passed is False
        assert "no merged product PR" in governing.message

    def test_gate_has_no_occ_clone_or_receipt_parameters(self) -> None:
        import inspect

        parameters = set(inspect.signature(DurableEvidenceGate).parameters)
        assert parameters == {
            "release_tags_containing",
            "index_release_files",
            "read_repo_contract",
            "read_repo_check_runs",
        }

    def test_enforce_threads_done_class_labels(self) -> None:
        """enforce() passes label context through to the hard-fail gate."""
        gate = _make_gate(contract_in_repo=_ticket_contract())

        result = gate.enforce(
            ticket_id=_TICKET,
            contract=_ticket_contract(),
            ticket_labels=frozenset({EnumDoneClassLabel.SOURCE_DONE.value}),
            merged_prs=(_merged_pr(),),
            ticket_description=_DESCRIPTION,
        )

        assert _check(result, EnumDurableEvidenceCheck.DONE_CLASS_LABEL).passed is True

    def test_enforce_raises_with_structured_error(self) -> None:
        """enforce() raises DurableEvidenceGateError carrying the result."""
        gate = _make_gate(contract_in_repo=None)

        with pytest.raises(DurableEvidenceGateError) as exc_info:
            gate.enforce(
                ticket_id=_TICKET,
                contract=_ticket_contract(),
                merged_prs=(_merged_pr(),),
                ticket_description=_DESCRIPTION,
            )

        err = exc_info.value
        assert err.result.ticket_id == _TICKET
        assert err.result.status == EnumDurableEvidenceStatus.FAIL
        assert "contract_on_occ_main" in str(err)

    def test_plain_done_no_class_label_hard_fails(self) -> None:
        """Contract green but NO done-class label → HARD FAIL (OMN-13337)."""
        result = _evaluate(_make_gate(contract_in_repo=_ticket_contract()))

        assert result.status == EnumDurableEvidenceStatus.FAIL
        done_class = _check(result, EnumDurableEvidenceCheck.DONE_CLASS_LABEL)
        assert done_class.passed is False
        assert "no approved done-class label" in done_class.message
        assert all(
            c.passed
            for c in result.checks
            if c.check != EnumDurableEvidenceCheck.DONE_CLASS_LABEL
        )

    def test_non_done_class_label_does_not_satisfy(self) -> None:
        """A label outside the approved set does not count as a done-class."""
        result = _evaluate(
            _make_gate(contract_in_repo=_ticket_contract()),
            labels=frozenset({"bug", "needs-review"}),
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        assert _check(result, EnumDurableEvidenceCheck.DONE_CLASS_LABEL).passed is False

    def test_done_class_label_without_governing_contract_hard_fails(self) -> None:
        """A done-class label the repository contract does not back → HARD FAIL.

        The label alone is not evidence.
        """
        result = _evaluate(
            _make_gate(contract_in_repo=None),
            labels=frozenset({EnumDoneClassLabel.PROD_PROVEN.value}),
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        done_class = _check(result, EnumDurableEvidenceCheck.DONE_CLASS_LABEL)
        assert done_class.passed is False
        assert "not evidence" in done_class.message

    def test_done_class_label_backed_by_repo_contract_passes(self) -> None:
        result = _evaluate(
            _make_gate(contract_in_repo=_ticket_contract()),
            labels=frozenset({EnumDoneClassLabel.PROJECTION_BACKED.value}),
        )

        assert result.status == EnumDurableEvidenceStatus.PASS, result.checks
        done_class = _check(result, EnumDurableEvidenceCheck.DONE_CLASS_LABEL)
        assert done_class.passed is True
        assert EnumDoneClassLabel.PROJECTION_BACKED.value in done_class.message

    def test_done_class_label_values_are_the_six_canonical(self) -> None:
        """The approved done-class set is exactly the six canonical labels."""
        assert EnumDoneClassLabel.values() == frozenset(
            {
                "source-done",
                "runtime-observed",
                "projection-backed",
                "replay-proven",
                "demo-visible",
                "prod-proven",
            }
        )

    def test_defect_ticket_without_prevention_hard_fails(self) -> None:
        """A defect-labelled ticket with no prevention gate / note -> HARD FAIL."""
        result = _evaluate(
            _make_gate(contract_in_repo=_ticket_contract()),
            labels=frozenset(
                {EnumDefectLabel.BUG.value, EnumDoneClassLabel.SOURCE_DONE.value}
            ),
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        defect = _check(result, EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE)
        assert defect.passed is False
        assert "cannot close" in defect.message
        assert all(
            c.passed
            for c in result.checks
            if c.check != EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE
        )

    def test_defect_ticket_with_prevention_gate_passes(self) -> None:
        """A defect ticket whose repository contract links a prevention gate -> PASS."""
        contract = dict(_ticket_contract())
        contract["prevention_gate"] = (
            ".github/workflows/validator-no-except-swallow.yml"
        )
        result = _evaluate(
            _make_gate(contract_in_repo=contract),
            labels=frozenset(
                {
                    EnumDefectLabel.REGRESSION.value,
                    EnumDoneClassLabel.PROJECTION_BACKED.value,
                }
            ),
        )

        assert result.status == EnumDurableEvidenceStatus.PASS, result.checks
        defect = _check(result, EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE)
        assert defect.passed is True
        assert "prevention_gate" in defect.message

    def test_defect_ticket_with_non_recurrence_note_passes(self) -> None:
        contract = dict(_ticket_contract())
        contract["non_recurrence_note"] = "One-off data repair; no automated gate."
        result = _evaluate(
            _make_gate(contract_in_repo=contract),
            labels=frozenset(
                {EnumDefectLabel.DEFECT.value, EnumDoneClassLabel.SOURCE_DONE.value}
            ),
        )

        assert result.status == EnumDurableEvidenceStatus.PASS, result.checks
        defect = _check(result, EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE)
        assert defect.passed is True
        assert "non_recurrence_note" in defect.message

    def test_blank_prevention_fields_do_not_satisfy(self) -> None:
        """Whitespace-only prevention fields are treated as absent -> HARD FAIL."""
        contract = dict(_ticket_contract())
        contract["prevention_gate"] = "   "
        contract["non_recurrence_note"] = ""
        result = _evaluate(
            _make_gate(contract_in_repo=contract),
            labels=frozenset(
                {EnumDefectLabel.BUG.value, EnumDoneClassLabel.SOURCE_DONE.value}
            ),
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        assert (
            _check(result, EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE).passed
            is False
        )

    def test_non_defect_ticket_is_exempt_from_ratchet(self) -> None:
        """A non-defect ticket passes the ratchet check N/A even with no fields."""
        result = _evaluate(
            _make_gate(contract_in_repo=_ticket_contract()),
            labels=frozenset({EnumDoneClassLabel.DEMO_VISIBLE.value}),
        )

        assert result.status == EnumDurableEvidenceStatus.PASS, result.checks
        defect = _check(result, EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE)
        assert defect.passed is True
        assert "does not apply" in defect.message

    def test_defect_label_values_are_canonical(self) -> None:
        """The defect-class label set is exactly the three canonical labels."""
        assert EnumDefectLabel.values() == frozenset({"bug", "defect", "regression"})

    def test_default_contract_path_is_the_repository_layout(self) -> None:
        assert default_contract_path("OMN-12574") == "contracts/OMN-12574.yaml"


@pytest.mark.unit
class TestExtractReceiptMergeCommits:
    """extract_receipt_merge_commits is the parser the gate relies on."""

    def test_receipt_json_url_extracted(self) -> None:
        cites = extract_receipt_merge_commits([_receipt()])
        assert len(cites) == 1
        assert cites[0].pr_number == 949
        assert cites[0].repo == "OmniNode-ai/omnibase_core"
        assert cites[0].evidence_item_id == "dod-001"
        assert cites[0].check_type == "command"

    def test_receipt_probe_command_repo_extracted(self) -> None:
        receipt = _receipt(repo="OmniNode-ai/omnimarket", pr_number=123) | {
            "probe_stdout": "non-json output"
        }
        cites = extract_receipt_merge_commits([receipt])
        assert len(cites) == 1
        assert cites[0].pr_number == 123
        assert cites[0].repo == "OmniNode-ai/omnimarket"

    def test_dedupes_repeated_receipts(self) -> None:
        receipt = _receipt(pr_number=1, commit_sha="aaa1111")
        cites = extract_receipt_merge_commits([receipt, receipt])
        assert len(cites) == 1

    def test_skips_malformed_or_nonpass_receipts(self) -> None:
        cites = extract_receipt_merge_commits(
            [
                {"evidence_item_id": "dod-001"},
                _receipt(status="FAIL"),
                _receipt(commit_sha="abc"),
                _receipt() | {"pr_number": None},
                _receipt() | {"probe_stdout": "non-json", "probe_command": "true"},
            ]
        )
        assert cites == []

    def test_url_variants_dedupe_to_same_citation(self) -> None:
        receipt_a = _receipt(pr_number=123, commit_sha="deadbeef1234567")
        receipt_b = _receipt(pr_number=123, commit_sha="deadbeef1234567") | {
            "probe_stdout": (
                '{"number":123,"url":"https://github.com/OmniNode-ai/'
                'omnibase_core/pull/123/files","state":"MERGED",'
                '"mergeCommit":{"oid":"deadbeef1234567"}}'
            )
        }

        cites = extract_receipt_merge_commits([receipt_a, receipt_b])

        assert len(cites) == 1
        assert cites[0].repo == "OmniNode-ai/omnibase_core"
        assert cites[0].pr_number == 123
        assert cites[0].cited_sha == "deadbeef1234567"


@pytest.mark.unit
class TestParsePrUrl:
    """parse_pr_url helper isolates the URL grammar."""

    def test_parses_canonical_pr_url(self) -> None:
        assert parse_pr_url(
            "https://github.com/OmniNode-ai/omnibase_core/pull/949"
        ) == ("OmniNode-ai/omnibase_core", 949)

    def test_parses_pr_url_with_trailing_path(self) -> None:
        assert parse_pr_url(
            "https://github.com/OmniNode-ai/omnimarket/pull/123/files"
        ) == ("OmniNode-ai/omnimarket", 123)

    def test_parse_pr_url_handles_invalid(self) -> None:
        assert parse_pr_url("not-a-url") is None
        assert parse_pr_url("https://github.com/owner/repo/issues/1") is None
        assert parse_pr_url("") is None


_OCC = "OmniNode-ai/onex_change_control"
_CAPTURED_SHA = "6324fe84debbb85e100348195b48357d8ef14f24"


def _captured_receipt(
    *,
    probe_repo: str = _OCC,
    probed_sha: str = _CAPTURED_SHA,
    commit_sha: str = _CAPTURED_SHA,
    pr_number: int = 13213,
) -> dict[str, object]:
    """A receipt in the occ-probe-capture-v1 shape (OMN-16106).

    The writer names the repository only through the GitHub REST path of its
    commit probe: no ``--repo`` flag and no ``/pull/`` URL anywhere.
    """
    return _receipt(pr_number=pr_number, commit_sha=commit_sha) | {
        "check_type": "test_passes",
        "check_value": "uv run pytest tests/unit/scripts/test_probe.py -q",
        "verifier": "occ-probe-capture-v1",
        "probe_command": (
            f"gh api repos/{probe_repo}/commits/{probed_sha} --jq .sha || exit 2\n"
            f'test "$(git rev-parse HEAD)" = {probed_sha} || exit 2\n'
            "uv run pytest tests/unit/scripts/test_probe.py -q"
        ),
        "probe_stdout": f"{probed_sha}\n....  [100%]\n4 passed in 0.23s",
    }


@pytest.mark.unit
class TestReceiptRepoFromGithubApiPath:
    """OMN-16106: a receipt that names its repo only by a GitHub REST path.

    Every receipt written by occ-probe-capture-v1 carries ``pr_number`` and
    ``commit_sha`` and names its repository only as
    ``gh api repos/<owner>/<repo>/commits/<commit_sha>``. The parser read
    neither, so the closer reported "No PASS receipt ... binds a GitHub repo"
    on tickets whose every other check passed.
    """

    def test_commit_probe_path_names_the_repo(self) -> None:
        cites = extract_receipt_merge_commits([_captured_receipt()])
        assert len(cites) == 1
        assert cites[0].repo == _OCC
        assert cites[0].pr_number == 13213
        assert cites[0].cited_sha == _CAPTURED_SHA

    def test_pulls_path_for_the_receipt_pr_names_the_repo(self) -> None:
        receipt = _captured_receipt() | {
            "probe_command": f"gh api repos/{_OCC}/pulls/13213 --jq .merged",
            "probe_stdout": "true",
        }
        cites = extract_receipt_merge_commits([receipt])
        assert [(c.repo, c.pr_number) for c in cites] == [(_OCC, 13213)]

    def test_commit_probe_of_another_sha_names_no_repo(self) -> None:
        receipt = _captured_receipt(probed_sha="1111111" + "a" * 33)
        assert extract_receipt_merge_commits([receipt]) == []

    def test_pulls_path_of_another_pr_names_no_repo(self) -> None:
        receipt = _captured_receipt() | {
            "probe_command": f"gh api repos/{_OCC}/pulls/132130 --jq .merged",
            "probe_stdout": "true",
        }
        assert extract_receipt_merge_commits([receipt]) == []

    def test_api_path_is_read_only_when_no_receipt_names_a_repo(self) -> None:
        explicit = _receipt()
        inferred = _captured_receipt() | {"evidence_item_id": "dod-002"}
        cites = extract_receipt_merge_commits([explicit, inferred])
        assert [(c.repo, c.pr_number) for c in cites] == [
            ("OmniNode-ai/omnibase_core", 949)
        ]
