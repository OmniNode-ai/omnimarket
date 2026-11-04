# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18010: the durable-evidence gate refuses closure of merged-but-unreleased work.

The gate this file exercises is the OMN-13856 pre-Linear-Done surface. Before
OMN-18010 it asked whether the evidence PR was MERGED and stopped there, so a
ticket whose change landed in a repo that publishes to a package index — and
was never released — passed every check. That is the measured failure:
``omnimarket#2304`` merged 2026-09-05 and sat unreleased with its own release
ticket in Backlog; ``#2334`` likewise.

Hermetic: both released probes are stubs replaying the recordings documented in
``test_omn_18010_released_evidence.py``. No network, no clone.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDoneClassLabel,
    EnumDurableEvidenceCheck,
    EnumDurableEvidenceStatus,
    EnumRepoContractReadStatus,
    ModelRepoContractRead,
    ModelRepoEvidenceCheckRun,
    ModelTicketMergedPr,
)
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    REPO_EVIDENCE_APP_SLUG,
    REPO_EVIDENCE_CHECK_NAME,
    DurableEvidenceGate,
    DurableEvidenceGateError,
)

_TICKET = "OMN-18010"
_DESCRIPTION = (
    "## Acceptance criteria\n\n"
    "- [ ] AC1: the released check -- falsifier: uv run pytest t.py\n"
)

# The squash merge commit GitHub reports for the cited PR. A release tag can
# contain THIS sha; it can never contain the pre-merge head sha, because these
# repos are squash-merge-only.
_MERGE_OID = "021b29cacc8c5711ea83d132df63f30e10b26fca"
_HEAD_SHA = "7287a42c1111111111111111111111111111beef"


def _contract() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "dod_evidence": [
            {
                "id": "released-probe",
                "description": "the released check",
                "checks": [
                    {"check_type": "test_passes", "check_value": "uv run pytest t.py"}
                ],
                "binds_ac": ["AC1"],
            }
        ],
    }


def _merged_pr(repo: str, pr_number: int, commit_sha: str, merge_oid: str):
    return ModelTicketMergedPr(
        repo=repo,
        pr_number=pr_number,
        head_sha=commit_sha,
        merge_commit_sha=merge_oid,
        merged_at="2026-10-09T12:00:00Z",
    )


def _make_gate(
    *,
    released_tags: dict[tuple[str, str], tuple[str, ...] | None],
    released_index: dict[tuple[str, str], frozenset[str] | None],
) -> DurableEvidenceGate:
    """A gate whose product repository carries a green contract for the ticket."""
    contract = _contract()

    def read_repo_contract(
        repo: str, ref: str, ticket_id: str
    ) -> ModelRepoContractRead:
        return ModelRepoContractRead(
            status=EnumRepoContractReadStatus.FOUND, contract=contract
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

    def tags_probe(r: str, sha: str) -> tuple[str, ...] | None:
        if (r, sha) not in released_tags:
            msg = f"unrecorded tag probe: {r}@{sha}"
            raise AssertionError(msg)
        return released_tags[(r, sha)]

    def index_probe(distribution: str, version: str) -> frozenset[str] | None:
        if (distribution, version) not in released_index:
            msg = f"unrecorded index probe: {distribution} {version}"
            raise AssertionError(msg)
        return released_index[(distribution, version)]

    return DurableEvidenceGate(
        release_tags_containing=tags_probe,
        index_release_files=index_probe,
        read_repo_contract=read_repo_contract,
        read_repo_check_runs=read_repo_check_runs,
    )


def _evaluate(gate: DurableEvidenceGate, repo: str, pr_number: int):
    return gate.evaluate(
        ticket_id=_TICKET,
        contract=_contract(),
        ticket_labels=frozenset({EnumDoneClassLabel.SOURCE_DONE.value}),
        merged_prs=(_merged_pr(repo, pr_number, _HEAD_SHA, _MERGE_OID),),
        ticket_description=_DESCRIPTION,
    )


def _released_check(result):
    return next(
        c
        for c in result.checks
        if c.check is EnumDurableEvidenceCheck.RELEASED_ON_PUBLISHING_REPO
    )


@pytest.mark.unit
class TestReleasedGate:
    """The gate's new non-closing state."""

    def test_merged_but_unreleased_is_refused(self) -> None:
        """Every other check green; the merge is in no release tag => FAIL."""
        gate = _make_gate(
            released_tags={("OmniNode-ai/omnimarket", _MERGE_OID): ()},
            released_index={},
        )
        result = _evaluate(gate, "OmniNode-ai/omnimarket", 2370)

        assert result.status == EnumDurableEvidenceStatus.FAIL
        check = _released_check(result)
        assert not check.passed
        assert "MERGED_UNRELEASED" in check.message
        # The product-repository verdict itself is UNAFFECTED: the contract is
        # governed and green. The two defects stay distinguishable, which is the
        # point: the remediation for one is a repo-evidence fix and for the
        # other a release cut.
        governing = next(
            c
            for c in result.checks
            if c.check is EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN
        )
        assert governing.passed

    def test_released_merge_passes(self) -> None:
        """Positive control for the case above: same shape, a containing tag."""
        gate = _make_gate(
            released_tags={("OmniNode-ai/omnimarket", _MERGE_OID): ("v0.4.22",)},
            released_index={
                ("omnimarket", "0.4.22"): frozenset({"bdist_wheel", "sdist"})
            },
        )
        result = _evaluate(gate, "OmniNode-ai/omnimarket", 2369)

        assert result.status == EnumDurableEvidenceStatus.PASS
        check = _released_check(result)
        assert check.passed
        assert "v0.4.22" in check.message

    def test_the_probe_is_asked_about_the_merge_commit_not_the_head_sha(self) -> None:
        """A squash merge's head sha has no ancestry to the tag; the oid does.

        If the gate passed ``cited_sha`` the tag probe would be asked about
        ``_HEAD_SHA``, which the stub is not primed for and would raise. This
        test therefore fails loudly on that regression rather than silently
        reporting every squash-merged ticket as unreleased.
        """
        gate = _make_gate(
            released_tags={("OmniNode-ai/omnimarket", _MERGE_OID): ("v0.4.22",)},
            released_index={
                ("omnimarket", "0.4.22"): frozenset({"bdist_wheel", "sdist"})
            },
        )
        assert _released_check(_evaluate(gate, "OmniNode-ai/omnimarket", 2369)).passed

    def test_non_publishing_repo_does_not_apply(self) -> None:
        """A merge into a repository that publishes nothing, so it is exempt.

        The tag/index stubs are primed with NOTHING here: if the gate probed
        them for a non-publishing repo the stub would raise. That is the
        assertion — the check is skipped by registry membership, not by luck.
        """
        gate = _make_gate(
            released_tags={},
            released_index={},
        )
        result = _evaluate(gate, "OmniNode-ai/onex_change_control", 8519)

        assert result.status == EnumDurableEvidenceStatus.PASS
        check = _released_check(result)
        assert check.passed
        assert "does not apply" in check.message

    def test_indeterminate_probe_refuses_rather_than_certifies(self) -> None:
        """An unfetched clone is not a release. Fail-closed."""
        gate = _make_gate(
            released_tags={("OmniNode-ai/omnibase_core", _MERGE_OID): None},
            released_index={},
        )
        result = _evaluate(gate, "OmniNode-ai/omnibase_core", 1698)

        assert result.status == EnumDurableEvidenceStatus.FAIL
        check = _released_check(result)
        assert not check.passed
        assert "INDETERMINATE" in check.message

    def test_tag_without_a_published_distribution_is_refused(self) -> None:
        gate = _make_gate(
            released_tags={("OmniNode-ai/omnibase_core", _MERGE_OID): ("v0.99.99",)},
            released_index={("omnibase-core", "0.99.99"): frozenset()},
        )
        result = _evaluate(gate, "OmniNode-ai/omnibase_core", 1698)

        assert result.status == EnumDurableEvidenceStatus.FAIL
        assert "RELEASED_TAG_NOT_ON_INDEX" in _released_check(result).message

    def test_enforce_raises_on_an_unreleased_merge(self) -> None:
        """The hard-fail entry point refuses, it does not merely report."""
        gate = _make_gate(
            released_tags={("OmniNode-ai/omnimarket", _MERGE_OID): ()},
            released_index={},
        )
        with pytest.raises(DurableEvidenceGateError):
            gate.enforce(
                ticket_id=_TICKET,
                contract=_contract(),
                ticket_labels=frozenset({EnumDoneClassLabel.SOURCE_DONE.value}),
                merged_prs=(
                    _merged_pr("OmniNode-ai/omnimarket", 2370, _HEAD_SHA, _MERGE_OID),
                ),
                ticket_description=_DESCRIPTION,
            )

    def test_the_released_probes_are_required_constructor_arguments(self) -> None:
        """There is no unconfigured path that silently skips the check.

        CLAUDE.md rule 5: a check that is opt-in is a check that is ignored.
        Omitting the probes must be a TypeError at construction, not a pass.
        """
        with pytest.raises(TypeError):
            DurableEvidenceGate(  # type: ignore[call-arg]
                read_repo_contract=lambda *_a, **_k: ModelRepoContractRead(
                    status=EnumRepoContractReadStatus.ABSENT
                ),
                read_repo_check_runs=lambda *_a, **_k: (),
            )
