# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The install-first verdict (OMN-21815, OMN-19998): which changed files need install proof, and what proof counts.

Failing controls: with the declaration as it stood before the change (no CI-only exclusion) a scripts/ci file requires
install proof; with the change it does not. The error chain: a missing proof, an unread ancestry and a head that moved are
each a NO with its own reason, never an OK.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_handoff_lab_exempt_compute.handlers.handler_install_verdict import (
    HandlerInstallVerdict,
    candidate_revisions,
    install_files,
    status_rows,
)
from omnimarket.nodes.node_pr_handoff_lab_exempt_compute.models.model_install_declaration import (
    ModelPrHandoffInstallDeclaration,
)
from omnimarket.nodes.node_pr_handoff_lab_exempt_compute.models.model_install_verdict import (
    ModelInstallAncestry,
    ModelInstallChangedFile,
    ModelInstallSource,
    ModelInstallVerdictRequest,
)
from tests.nodes.node_pr_handoff_lab_exempt_compute.declaration_fixture import (
    BASE_EXCLUDED,
    declaration,
)

HEAD = "c3750094076ae28793a971850c4e45d03c2a74cb"
REPO = "omnimarket"


def _decl(excluded: list[str] | None = None) -> ModelPrHandoffInstallDeclaration:
    return ModelPrHandoffInstallDeclaration.model_validate(declaration(excluded))


def _files(*names: str) -> tuple[ModelInstallChangedFile, ...]:
    return tuple(ModelInstallChangedFile(filename=n) for n in names)


def _request(
    *names: str, excluded: list[str] | None = None, **extra: object
) -> ModelInstallVerdictRequest:
    return ModelInstallVerdictRequest(
        declaration=_decl(excluded), repo=REPO, head=HEAD, files=_files(*names), **extra
    )  # type: ignore[arg-type]


LAB = f"Lab: head={HEAD[:12]} host=h051 installed=h051:{HEAD[:12]} observed=the tick ran and exit 0 was printed"


def test_ci_only_script_needs_no_install_proof() -> None:
    verdict = HandlerInstallVerdict().handle(
        _request(
            "scripts/ci/check_unimported_handlers.py", "scripts/validation/baseline.txt"
        )
    )
    assert verdict.status == "NA"
    assert verdict.line.startswith("NA no changed file is a host install")


def test_nested_ci_only_folders_need_no_install_proof() -> None:
    assert (
        HandlerInstallVerdict()
        .handle(_request("tools/scripts/ci/a.py", "tools/scripts/validation/b.py"))
        .status
        == "NA"
    )


def test_control_declaration_without_the_ci_exclusion_still_requires_proof() -> None:
    """The failing control: the same file under the declaration as it was before the change needs install proof."""
    verdict = HandlerInstallVerdict().handle(
        _request("scripts/ci/check_unimported_handlers.py", excluded=BASE_EXCLUDED)
    )
    assert verdict.status == "NO"
    assert verdict.required_files == ("scripts/ci/check_unimported_handlers.py",)


def test_other_scripts_still_need_install_proof() -> None:
    verdict = HandlerInstallVerdict().handle(_request("scripts/other.sh"))
    assert verdict.status == "NO"
    assert verdict.required_files == ("scripts/other.sh",)


def test_a_scripts_file_beside_the_ci_folder_still_needs_proof() -> None:
    assert install_files(
        _files("scripts/cicd.sh", "scripts/validation_helpers/x.sh"), REPO, _decl()
    ) == ["scripts/cicd.sh", "scripts/validation_helpers/x.sh"]


def test_a_host_unit_file_still_needs_install_proof() -> None:
    verdict = HandlerInstallVerdict().handle(
        _request("launchd/ai.example.plist", "systemd/unit.service")
    )
    assert verdict.status == "NO"
    assert verdict.required_files == (
        "launchd/ai.example.plist",
        "systemd/unit.service",
    )


def test_a_mixed_change_names_only_the_files_that_run_on_a_host() -> None:
    verdict = HandlerInstallVerdict().handle(
        _request("scripts/ci/a.py", "scripts/run.sh", "docs/x.md")
    )
    assert verdict.required_files == ("scripts/run.sh",)


def test_a_prod_path_is_never_an_install_requirement() -> None:
    assert (
        HandlerInstallVerdict().handle(_request("scripts/prod_promotion_x.sh")).status
        == "NA"
    )


def test_golden_chain_a_lab_line_with_the_live_head_installed_and_observed_passes() -> (
    None
):
    verdict = HandlerInstallVerdict().handle(
        _request(
            "launchd/ai.example.plist",
            sources=(ModelInstallSource(name="body", text="Summary\n" + LAB),),
        )
    )
    assert verdict.status == "OK"
    assert verdict.detail == f"installed=h051:{HEAD[:12]} source=body"


def test_golden_chain_a_status_row_of_the_ticket_passes_with_a_descendant_install() -> (
    None
):
    row = f"2026-11-10T17:00:00Z | STATUS | lane=other | ticket=OMN-1 | installed=h051:{'a' * 12} | observed=the unit tick ran and exit 0 was printed"
    request = _request(
        "hooks/pre.sh",
        sources=(ModelInstallSource(name="status", text=row),),
        ancestry={"a" * 12: ModelInstallAncestry(verdict="yes")},
    )
    verdict = HandlerInstallVerdict().handle(request)
    assert verdict.status == "OK"
    assert verdict.detail.endswith(f"source=status contains-head={HEAD[:7]}")


def test_error_chain_no_proof_is_a_no_with_the_install_instruction() -> None:
    verdict = HandlerInstallVerdict().handle(_request("hooks/pre.sh"))
    assert verdict.status == "NO"
    assert (
        "no Lab line, lab comment or STATUS row carries installed=<host>:<revision>"
        in verdict.detail
    )
    assert "(hooks/pre.sh)" in verdict.detail


@pytest.mark.parametrize(
    ("ancestry", "expected"),
    [
        (ModelInstallAncestry(verdict="no"), "does not contain it"),
        (
            ModelInstallAncestry(verdict="unread", why="no clone"),
            "its ancestry is UNREAD (no clone)",
        ),
        (None, "its ancestry is UNREAD (the revision was not read)"),
    ],
)
def test_error_chain_a_revision_that_does_not_hold_the_head_never_passes(
    ancestry: ModelInstallAncestry | None, expected: str
) -> None:
    line = f"Lab: head={HEAD[:12]} installed=h051:{'b' * 12} observed=the tick ran and exit 0 was printed"
    verdict = HandlerInstallVerdict().handle(
        _request(
            "hooks/pre.sh",
            sources=(ModelInstallSource(name="body", text=line),),
            ancestry={"b" * 12: ancestry} if ancestry else {},
        )
    )
    assert verdict.status == "NO"
    assert expected in verdict.detail


def test_error_chain_a_lab_line_for_an_older_head_never_passes() -> None:
    line = f"Lab: head={'d' * 12} installed=h051:{HEAD[:12]} observed=the tick ran and exit 0 was printed"
    verdict = HandlerInstallVerdict().handle(
        _request("hooks/pre.sh", sources=(ModelInstallSource(name="body", text=line),))
    )
    assert verdict.status == "NO"
    assert f"the Lab line names head {'d' * 7}" in verdict.detail


@pytest.mark.parametrize("observed", ["12 tests passed", "ok", "fine and good enough"])
def test_error_chain_an_observation_that_names_no_run_never_passes(
    observed: str,
) -> None:
    line = f"Lab: head={HEAD[:12]} installed=h051:{HEAD[:12]} observed={observed}"
    verdict = HandlerInstallVerdict().handle(
        _request("hooks/pre.sh", sources=(ModelInstallSource(name="body", text=line),))
    )
    assert verdict.status == "NO"
    assert "names no run" in verdict.detail


def test_an_exempt_kind_needs_no_proof() -> None:
    verdict = HandlerInstallVerdict().handle(
        _request("hooks/pre.sh", lab_exempt_kind="pin-refresh")
    )
    assert verdict.line == "NA the PR is an exempt kind (pin-refresh)"


def test_a_comment_proof_counts_after_the_body_and_status_rows() -> None:
    verdict = HandlerInstallVerdict().handle(
        _request(
            "hooks/pre.sh",
            comment_sources=(ModelInstallSource(name="comment", text=LAB),),
        )
    )
    assert verdict.status == "OK"
    assert verdict.detail.endswith("source=comment")


def test_candidate_revisions_are_the_installed_revisions_that_are_not_the_head() -> (
    None
):
    sources = (
        ModelInstallSource(name="body", text=LAB),
        ModelInstallSource(
            name="body", text=f"Lab: installed=h053:{'e' * 10} observed=x"
        ),
        ModelInstallSource(
            name="status", text=f"x | STATUS | installed=h051:{'e' * 10}"
        ),
    )
    assert candidate_revisions(sources, HEAD) == ["e" * 10]


def test_status_rows_keep_only_the_lane_or_ticket_with_an_installed_cell() -> None:
    ledger = "\n".join(
        [
            "t | STATUS | lane=mine | installed=h051:abcdef1 | observed=x",
            "t | STATUS | lane=other | ticket=OMN-9 | installed=h051:abcdef1",
            "t | STATUS | lane=other | ticket=OMN-1",
            "t | CLAIM | lane=mine | installed=h051:abcdef1",
        ]
    )
    assert len(status_rows(ledger, "mine", "OMN-9")) == 2


def test_declaration_refuses_a_blank_or_unknown_key() -> None:
    bad = declaration()
    bad["observed_refused"] = ["("]
    with pytest.raises(ValidationError):
        ModelPrHandoffInstallDeclaration.model_validate(bad)
    bad = declaration()
    del bad["excluded_paths"]
    with pytest.raises(ValidationError):
        ModelPrHandoffInstallDeclaration.model_validate(bad)
    bad = declaration()
    bad["surprise"] = 1
    with pytest.raises(ValidationError):
        ModelPrHandoffInstallDeclaration.model_validate(bad)
